"""
Infinite Ladder Grid Strategy (SymbolEngine)

Implements a Multi-Asset Grid with Leapfrog mechanics:
- Grid pairs with Buy/Sell at each level
- WAITING_CENTER phase: Both B1 and S1 must fill before expansion
- Leapfrog: Recycle pairs from one end to follow trends
- Re-open: TP/SL hit pairs are immediately reopened at same price
"""

import asyncio
import time
import json
import os
import threading
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, List, Any, Set 
from collections import defaultdict, deque
import asyncio
import time
import MetaTrader5 as mt5
from datetime import datetime, timedelta

from core.persistence.repository import Repository
from core.engine.group_logger import GroupLogger


@dataclass
class GridLevel:
    """Represents a single level in the grid ground truth"""
    level_number: int          # Internal level: ..., -2, -1, 0, 1, 2, ...
    buy_price: float          # Buy price at this level
    sell_price: float         # Sell price at this level (buy_price - spread)
    pair_index: int = 0       # User-facing pair index
    is_active: bool = False   # True if any position exists at this level
    
    def to_dict(self):
        return {
            "level_number": self.level_number,
            "buy_price": self.buy_price,
            "sell_price": self.sell_price,
            "pair_index": self.pair_index,
            "is_active": self.is_active
        }

@dataclass
class GridPair:
    """
    Represents a Buy/Sell pair at a specific grid level.
    
    Each pair has its own "brain" / memory:
    - trade_count: How many trades executed for THIS pair (used for lot sizing AND toggle)
    - next_action: Toggle state for buy→sell→buy sequence
    
    IMPORTANT: Lot sizing uses trade_count directly (sequential per pair, NOT per direction).
    """
    index: int                      # ..., -2, -1, 0, 1, 2, ...
    buy_price: float = 0.0          # Entry price for Buy order
    sell_price: float = 0.0         # Entry price for Sell order
    buy_ticket: int = 0             # MT5 order/position ticket (0 = not placed)
    sell_ticket: int = 0            # MT5 order/position ticket
    buy_filled: bool = False        # True if buy order has executed
    sell_filled: bool = False       # True if sell order has executed
    buy_pending_ticket: int = 0     # Pending order ticket for buy
    sell_pending_ticket: int = 0    # Pending order ticket for sell
    
    # Per-pair memory (THE "BRAIN")
    trade_count: int = 0            # Total trades executed in THIS pair (for LOT SIZING and toggle)
    next_action: str = "buy"        # Toggle: "buy" → "sell" → "buy" → ...
    first_fill_direction: str = ""  # Legacy: "buy" or "sell" - whichever filled first
    
    # Zone tracking for re-trigger (price must LEAVE and RETURN to re-trigger)
    buy_in_zone: bool = False       # True if price is currently at buy trigger level
    sell_in_zone: bool = False      # True if price is currently at sell trigger level
    is_reopened: bool = False       # True after TP/SL reset - bypasses zone checks
    
    # TP/SL Alignment: First trade of each 2-trade cycle sets these, second trade uses them inversely
    # Buy TP = Sell SL, Buy SL = Sell TP (determined by trade_count % 2)
    pair_tp: float = 0.0            # Shared TP level (set by first trade of cycle)
    pair_sl: float = 0.0            # Shared SL level (set by first trade of cycle)
    
    # Hedge System (Section 9)
    hedge_ticket: int = 0
    hedge_direction: str = None     # "buy" or "sell"
    hedge_active: bool = False      # True if hedge is currently open

    # New field for position age tracking (Bug 3 fix)
    position_timestamps: Dict[int, float] = field(default_factory=dict)  # ticket -> opening time
    
    # Group tracking: Explicitly track which group this pair belongs to
    # This fixes the issue where bearish expansion pairs (e.g., 99) were miscategorized
    group_id: int = 0  # Group 0 for first group, 1 for second group, etc.s
    
    locked_buy_entry: float = 0.0   # The actual execution price when BUY first fired
    locked_sell_entry: float = 0.0  # The actual execution price when SELL first fired
    tp_blocked: bool = False        # Permanent retirement flag (set on TP/SL)
    
    def get_next_lot(self, lot_sizes: list) -> float:
        """
        Get the next lot size for a trade based on trade_count.
        
        Lot sizing is SEQUENTIAL per pair (NO WRAPPING):
        - 1st trade (trade_count=0) → lot_sizes[0]
        - 2nd trade (trade_count=1) → lot_sizes[1]
        - etc.
        - After max_positions reached → returns None (blocked)
        """
        if not lot_sizes:
            return 0.01
        
        # HARD CAP: If trade_count >= number of lot sizes, return None to block trade
        if self.trade_count >= len(lot_sizes):
            return None
        
        return float(lot_sizes[self.trade_count])
    
    def advance_toggle(self):
        """Advance to next action in toggle sequence AND increment trade_count for lot sizing."""
        self.trade_count += 1
        self.next_action = "sell" if self.next_action == "buy" else "buy"
    
    # New methods for Bug 3 fix (1-second minimum position age)
    def record_position_open(self, ticket: int):
        """Record when position was opened for age tracking."""
        self.position_timestamps[ticket] = time.time()
    
    def get_position_age(self, ticket: int) -> float:
        """Get how long position has been open in seconds."""
        if ticket in self.position_timestamps:
            return time.time() - self.position_timestamps[ticket]
        return 0.0


class GridGroundTruth:
    """Maintains single source of truth for grid structure and pair indexing"""
    
    def __init__(self, symbol: str, spread: float):
        self.symbol = symbol
        self.spread = spread
        self.levels: Dict[int, GridLevel] = {}      # level_number -> GridLevel
        self.pair_to_level: Dict[int, int] = {}     # pair_index -> level_number
        self.center_level: int = 0
    
    def add_level(self, buy_price: float, sell_price: float, pair_index: int) -> int:
        """Add a new price level, return its internal level number"""
        # Calculate level number based on distance from existing levels
        if not self.levels:
            # First level, set as center
            level_num = 0
        else:
            # Find closest level
            closest_level = min(self.levels.values(), 
                              key=lambda l: abs(l.buy_price - buy_price))
            level_num = closest_level.level_number
            
            # Determine if above or below
            if buy_price > closest_level.buy_price:
                level_num += 1
            else:
                level_num -= 1
        
        # Create and store the level
        level = GridLevel(
            level_number=level_num,
            buy_price=buy_price,
            sell_price=sell_price,
            pair_index=pair_index
        )
        self.levels[level_num] = level
        self.pair_to_level[pair_index] = level_num
        
        # Update center if needed
        if level_num == 0:
            self.center_level = 0
        
        return level_num
    
    def price_to_level(self, price: float) -> Optional[int]:
        """Convert price to grid level number"""
        if not self.levels:
            return None
        
        # Find level with closest buy price
        closest_level = None
        min_diff = float('inf')
        
        for level_num, level in self.levels.items():
            diff = abs(level.buy_price - price)
            if diff < min_diff and diff < self.spread * 0.5:  # Within half spread
                min_diff = diff
                closest_level = level_num
        
        return closest_level
    
    def get_level_by_pair_index(self, pair_index: int) -> Optional[GridLevel]:
        """Get level by pair index"""
        level_num = self.pair_to_level.get(pair_index)
        if level_num is not None:
            return self.levels.get(level_num)
        return None
    
    def update_pair_index(self, old_index: int, new_index: int):
        """Update pair index for a level"""
        level_num = self.pair_to_level.get(old_index)
        if level_num is not None:
            level = self.levels.get(level_num)
            if level:
                level.pair_index = new_index
                del self.pair_to_level[old_index]
                self.pair_to_level[new_index] = level_num
                
    def get_correct_pair_index(self, buy_price: float, sell_price: float) -> int:
        """Get the correct pair index for a price level"""
        level_num = self.price_to_level(buy_price)
        if level_num is not None:
            level = self.levels.get(level_num)
            if level and abs(level.buy_price - buy_price) < self.spread * 0.5:
                return level.pair_index
        
        # If no existing level, calculate based on distance from center
        if self.levels:
            # Find the level with buy_price closest to center
            center_buy = self.levels[self.center_level].buy_price
            price_diff = buy_price - center_buy
            levels_from_center = round(price_diff / self.spread)
            return levels_from_center
        else:
            # No levels yet, assume this is center
            return 0
    
    def validate_and_correct(self, pairs: Dict[int, GridPair]) -> Dict[int, GridPair]:
        """Validate all pairs and correct indices if needed"""
        corrected_pairs = {}
        
        for idx, pair in pairs.items():
            # Get correct index for this pair's price level
            correct_idx = self.get_correct_pair_index(pair.buy_price, pair.sell_price)
            
            if idx != correct_idx:
                print(f"GRID CORRECTION: Pair at price {pair.buy_price:.2f} should be index {correct_idx}, not {idx}")
                
                # Update pair's index
                pair.index = correct_idx
                
                # Update ground truth mapping
                level_num = self.price_to_level(pair.buy_price)
                if level_num is None:
                    level_num = self.add_level(pair.buy_price, pair.sell_price, correct_idx)
                
                self.update_pair_index(idx, correct_idx)
            
            # Store in corrected dict
            corrected_pairs[pair.index] = pair
        
        return corrected_pairs
    
    def rebuild_from_positions(self, positions: list) -> Dict[int, int]:
        """Rebuild ground truth from MT5 positions, return pair_index -> level_number mapping"""
        if not positions:
            return {}
        
        # Clear existing data
        self.levels.clear()
        self.pair_to_level.clear()
        
        # Group positions by pair index (from magic number)
        position_groups = {}
        for pos in positions:
            if pos.magic >= 50000:
                pair_idx = pos.magic - 50000
                if pair_idx not in position_groups:
                    position_groups[pair_idx] = []
                position_groups[pair_idx].append(pos)
        
        # Build levels from positions
        level_mapping = {}
        for pair_idx, pos_list in position_groups.items():
            if not pos_list:
                continue
                
            # Use first position's open price as reference
            ref_pos = pos_list[0]
            buy_price = ref_pos.price_open if ref_pos.type == 0 else ref_pos.price_open + self.spread
            sell_price = buy_price - self.spread
            
            # Add to ground truth
            level_num = self.add_level(buy_price, sell_price, pair_idx)
            level_mapping[pair_idx] = level_num
        
        return level_mapping
    
    def print_debug(self):
        """Print ground truth for debugging"""
        print(f"\n{'='*60}")
        print(f"GRID GROUND TRUTH - {self.symbol}")
        print(f"{'='*60}")
        print(f"{'Level':>6} {'Pair Index':>10} {'Buy Price':>12} {'Sell Price':>12} {'Active':>6}")
        print(f"{'-'*60}")
        
        for level_num in sorted(self.levels.keys()):
            level = self.levels[level_num]
            print(f"{level_num:>6} {level.pair_index:>10} {level.buy_price:>12.2f} {level.sell_price:>12.2f} {str(level.is_active):>6}")
        
        print(f"{'='*60}\n")


@dataclass
class TradeLog:
    """
    Represents a single trade event for debug visualization.
    Tracks: order sequence, TP/SL hits, leapfrog events, reopens.
    """
    timestamp: str              # Human-readable timestamp
    event_type: str             # OPEN, TP_HIT, SL_HIT, LEAPFROG_UP, LEAPFROG_DOWN, REOPEN
    pair_index: int             # Which pair (e.g., -2, -1, 0, 1, 2)
    direction: str              # BUY or SELL
    price: float                # Entry/exit price
    lot_size: float             # Volume
    trade_num: int = 0          # Trade number within this pair's cycle
    ticket: int = 0             # MT5 ticket (if available)
    notes: str = ""             # Additional info (e.g., "from Pair -2")
    
    def __str__(self):
        return f"[{self.timestamp}] {self.event_type:<12} | Pair {self.pair_index:>2} | {self.direction:<4} @ {self.price:>10.2f} | Lot: {self.lot_size:.2f} | #{self.trade_num} | {self.notes}"





class SymbolEngine:
    """
    Multi-Asset Infinite-Ladder Grid Strategy with Leapfrog mechanics.
    
    Phases:
    - INIT: Place initial B1 (Buy Stop) and S1 (Sell Stop)
    - WAITING_CENTER: Wait for BOTH B1 and S1 to fill
    - EXPANDING: Add pairs until max_pairs reached
    - RUNNING: Monitor TP/SL, execute Leapfrog, re-open pairs
    """
    
    PHASE_INIT = "INIT"
    PHASE_WAITING_CENTER = "WAITING_CENTER"
    PHASE_EXPANDING = "EXPANDING"
    PHASE_RUNNING = "RUNNING"
    
    MAX_RETRY_ATTEMPTS = 5

    def __init__(self, config_manager, symbol: str, session_logger=None):
        self.config_manager = config_manager
        self.symbol = symbol
        self.session_logger = session_logger
        self.running = False
        
        # --- Persistence ---
        self.repository = Repository(symbol)
        self.db_path = "db/grid_v3.db"  # Path to DB for cleanup
        
        # --- Grid Ground Truth ---
        self.grid_truth = GridGroundTruth(symbol, self.spread)
        
        # --- Grid State ---
        self.phase = self.PHASE_INIT
        self.center_price: float = 0.0          # Anchor price (adjusts when first fill happens)
        self.pairs: Dict[int, GridPair] = {}    # Active pairs keyed by index
        self.iteration: int = 1                 # Cycle count
        self.init_step: int = 0                 # 0=Pending, 1=B0_Complete, 2=S1_Complete
        
        # --- Tracking ---
        self.current_price: float = 0.0
        self.open_positions_count: int = 0
        self.pending_orders_count: int = 0
        self.start_time: float = 0
        self.is_busy: bool = False              # Lock for order operations
        
        # --- Auto-restart tracking ---
        self.last_trade_time: float = 0         # Last time we had active trades
        self.no_trade_timeout: float = 10.0    # Seconds before auto-restart (10 seconds)
        
        # --- Debug Trade History (REMOVED - now in DB) ---
        self.global_trade_counter: int = 0               # Total trades across all pairs
        self.debug_log_file = f"trade_debug_{self.symbol.replace(' ', '_')}.txt"
        
        # --- Graceful Stop ---
        self.graceful_stop: bool = False    # When True, complete open pairs before stopping
        
        # --- History-Based TP/SL Detection ---
        self.last_deal_check_time: float = time.time()  # Track last history query time
        self.processed_deals: deque = deque(maxlen=1000)  # Auto-cleanup: keeps last 1000 deals only
        
        # --- Ticket-Based Drop Detection (replaces count-based) ---
        # Tickets are tracked via pair.buy_ticket, pair.sell_ticket and verified in _monitor_position_drops
        
        # --- MUTEX LOCKS (Race Condition Prevention) ---
        self.execution_lock = asyncio.Lock()       # Global lock for atomic B[n] + S[n+1] chains
        # self.pair_locks REMOVED - not used or can be simplified
        self.trade_in_progress: Dict[int, bool] = defaultdict(bool)  # Track which pairs are mid-trade
        
        # ========================================================================
        # GROUPS + TP-DRIVEN STRATEGY (Multi-Group Cycle Management)
        # ========================================================================
        # Group numbering: Group 0 = pairs 0-99, Group 1 = 100-199, etc.
        self.GROUP_OFFSET: int = 100              # Pair offset per group
        self.current_group: int = 0               # Active group being traded
        self.group_anchors: Dict[int, float] = {} # group_id -> anchor_price
        self.pending_init: bool = False           # True = incomplete TP hit but C < 3, queue INIT
        
        # High-Water Mark for C (Completed Pairs)
        # Tracks the maximum C ever reached for each group.
        # This ensures that even if pairs close (dropping live C), the group progression logic
        # knows it has already achieved a certain level of completion.
        self.group_c_highwater: Dict[int, int] = defaultdict(int)
        
        # Legacy fields (maintained for compatibility)
        self.cycle_id: int = 0                    # Maps to current_group for now
        self.anchor_price: float = 0.0            # Per-cycle anchor (startup or TP price)
        self.group_direction: Optional[str] = None # "BULLISH" or "BEARISH"
        self.tolerance: float = 5.0               # T = ±5 fixed trigger tolerance
        self.bot_magic_base: int = 50000          # Base magic number for orders
        
        # Step trigger tracking - SEPARATE for bullish and bearish directions
        # This allows price reversals to properly trigger the other direction's ladder
        self.step1_bullish_triggered: bool = False
        self.step1_bearish_triggered: bool = False
        self.step2_bullish_triggered: bool = False
        self.step2_bearish_triggered: bool = False
        
        # Legacy flags (kept for compatibility, now derived)
        self.step1_triggered: bool = False
        self.step2_triggered: bool = False
        
        # TICKET TRACKING FOR DETERMINISTIC TP/SL DETECTION
        # Ticket → (pair_index, leg, entry_price, tp_price, sl_price) map
        self.ticket_map: Dict[int, tuple] = {}    # Runtime cache, persisted to DB

        # TP/SL touch tracking: ticket -> {'tp_touched': bool, 'sl_touched': bool}
        # Latched on every tick when price crosses TP/SL levels
        self.ticket_touch_flags: Dict[int, Dict[str, bool]] = {}

        # TP EXPANSION LOCK: Track pairs that have already fired expansion after TP
        # Once a completed pair hits TP and fires expansion, it is PERMANENTLY blocked
        # from firing expansion again (prevents grid inconsistency from repeated TP events)
        # pair_idx set - if pair is in this set, expansion is blocked
        self._pairs_tp_expanded: Set[int] = set()
        
        # INCOMPLETE PAIR INIT LOCK: Track incomplete pairs that have already fired INIT
        # Prevents duplicate INITs when toggle-trading creates multiple positions of same pair
        # Once an incomplete pair fires INIT, subsequent TP hits on that pair are blocked
        self._incomplete_pairs_init_triggered: Set[int] = set()
        
        # Graceful Stop Feature:
        # When True, NO NEW GROUPS will be created.
        # Existing pairs will continue trading until max_positions limit is reached.
        # Once all active pairs hit max_positions, the bot stops.
        self.graceful_stop: bool = False

        # ========================================================================
        # GROUP LOGGER - Structured per-group logging with table formatting
        # ========================================================================
        # Get user_id from session_logger if available
        user_id = session_logger.user_id if session_logger else None
        self.group_logger = GroupLogger(symbol=symbol, log_dir="logs", user_id=user_id)

        # ========================================================================
        # RETRACEMENT TRACKING - For natural expansion after INIT
        # ========================================================================
        # Tracks init source direction per group ("BULLISH" or "BEARISH")
        self.group_init_source: Dict[int, str] = {}

        # Pending retracement direction is OPPOSITE of init source
        # If init was bullish (buy TP), expect bearish retracement
        # If init was bearish (sell TP), expect bullish retracement
        self.group_pending_retracement: Dict[int, str] = {}

        # Anchor price where INIT fired (for calculating retracement levels)
        self.group_retracement_anchor: Dict[int, float] = {}

        # Track which retracement levels have been fired for each group
        # group_id -> set of level numbers (1, 2, 3...) that have expanded
        self.group_retracement_levels_fired: Dict[int, Set[int]] = defaultdict(set)

    @property
    def config(self) -> Dict[str, Any]:
        """Get symbol-specific config from the new multi-asset structure"""
        sym_config = self.config_manager.get_symbol_config(self.symbol)
        if sym_config:
            return sym_config
        # Fallback to global if symbol not found
        return self.config_manager.get_config().get('global', {})
    
    @property
    def lot_sizes(self) -> List[float]:
        """Get lot sizes list for this symbol"""
        return self.config.get('lot_sizes', [0.01])
    
    @property
    def spread(self) -> float:
        return float(self.config.get('spread', 20.0))
    
    @property
    def max_pairs(self) -> int:
        """Grid levels: 1, 3, 5, 7, or 9"""
        return int(self.config.get('max_pairs', 5))
    
    @property
    def max_positions(self) -> int:
        """Trades per pair: 1-20 (controls lot_sizes length)"""
        return int(self.config.get('max_positions', 5))

    @property
    def hedge_enabled(self) -> bool:
        return self.config.get('hedge_enabled', True)

    @property
    def hedge_lot_size(self) -> float:
        return float(self.config.get('hedge_lot_size', 0.01))
    
    # ========================================================================
    # PRICE-ANCHORED PAIR INDEX CALCULATION
    # ========================================================================
    
    def _calculate_pair_index_from_price(self, price: float, direction: str) -> int:
        """
        Calculate the correct pair index from price using center_price as anchor.
        
        PRICE IS THE SINGLE SOURCE OF TRUTH FOR PAIR INDEX.
        
        This method ensures that every position's pair index is derived from its
        execution price, not from mutable state. This prevents pair mislabeling
        after TP/SL re-entry.
        
        Grid structure:
        - B(n) is at center_price + (n * spread)
        - S(n) is at B(n) - spread = center_price + (n * spread) - spread
        
        Args:
            price: The execution/trigger price
            direction: "buy" or "sell"
        
        Returns:
            The correct pair index based on price position in the grid
        """
        if self.center_price == 0.0:
            return 0  # No anchor yet, return 0 for initial pair
        
        if direction == "buy":
            # Buy price directly maps to pair index
            # pair_idx = (buy_price - center_price) / spread
            pair_idx = round((price - self.center_price) / self.spread)
        else:
            # Sell price is one spread below the buy price at the same pair level
            # So: sell_price = center_price + (pair_idx * spread) - spread
            # Rearranging: pair_idx = (sell_price + spread - center_price) / spread
            #                       = (sell_price - center_price) / spread + 1
            pair_idx = round((price - self.center_price) / self.spread) + 1
        
        return int(pair_idx)
    
    # ========================================================================
    # GROUPS + 3-COMPLETED CAP STRATEGY (Core Methods)
    # ========================================================================
    
    def _count_completed_pairs_open(self) -> int:
        """
        Count completed pairs (both BUY and SELL positions exist).
        Uses ticket_map to determine pair membership.
        Returns count across ALL cycles.
        """
        positions = mt5.positions_get(symbol=self.symbol)
        if not positions:
            return 0
        
        
        # Group by pair_index using ticket_map
        pair_legs = defaultdict(set)  # pair_index → {'B', 'S'}
        for pos in positions:
            info = self.ticket_map.get(pos.ticket)
            if info:
                _, pair_idx, leg = info
                pair_legs[pair_idx].add(leg)
        
        # Count pairs with both legs
        completed = sum(1 for legs in pair_legs.values() if 'B' in legs and 'S' in legs)
        return completed
    
    def _is_pair_completed(self, pair_index: int) -> bool:
        """Check if a specific pair has both B and S positions open."""
        positions = mt5.positions_get(symbol=self.symbol)
        if not positions:
            return False
        
        legs = set()
        for pos in positions:
            info = self.ticket_map.get(pos.ticket)
            if info and info[1] == pair_index:
                legs.add(info[2])
        
        return 'B' in legs and 'S' in legs
    
    def _can_place_completing_leg(self, pair_index: int, leg: str) -> bool:
        """
        Group-Specific Lock Gate: Returns False if placing this leg would push C > 3 for THIS group.
        
        IMPORTANT DISTINCTION:
        - BLOCK: Trades that would COMPLETE an INCOMPLETE pair (creating a NEW completed pair)
        - ALLOW: Toggle trades on ALREADY COMPLETE pairs (they don't increase C)
        
        FIX: Uses group-specific C counting, not global count!
        This allows Group 1 to expand even when Group 0 has 3 completed pairs.
        
        Args:
            pair_index: The pair this order belongs to
            leg: 'B' or 'S'
        
        Returns:
            True if order can proceed, False if blocked by cap
        """
        pair = self.pairs.get(pair_index)
        if not pair:
            return True  # New pair creation blocked by step triggers, not here
        
        # Get group-specific C count (not global!)
        group_id = pair.group_id
        C = self._count_completed_pairs_for_group(group_id)
        # If already at cap (C >= 3), check if this would complete an INCOMPLETE pair
        if C >= 3:
            # EXCEPTION: If this trade will bring us to (or above) max_positions, ALLOW it.
            # This is critical for small max_positions (e.g. 2) where the "completing" leg
            # is also the "hedging" leg. If we block it, we prevent the hedge.
            # Since hedging neutralizes the pair (removing it from C calculation), 
            # this trade is effectively neutral to risk.
            if (pair.trade_count + 1) >= self.max_positions:
                return True

            # Check if pair is currently incomplete (only one leg filled)
            pair_is_incomplete = pair.buy_filled != pair.sell_filled
            
            if pair_is_incomplete:
                # This trade would complete an incomplete pair → BLOCK
                print(f"[CAP_BLOCK] pair={pair_index} leg={leg} BLOCKED (would complete incomplete pair, Group={group_id} C={C})")
                return False
            
            # Pair is already complete → ALLOW toggle trades
            # (This doesn't increase C, just continues trading on existing pair)
        
        return True
    
    def _is_locked(self) -> bool:
        """Global lock: True when 3+ completed pairs exist."""
        return self._count_completed_pairs_open() >= 3
    
    # ========================================================================
    # GROUP HELPER METHODS (TP-Driven Multi-Group System)
    # ========================================================================
    
    def _get_group_from_pair(self, pair_idx: int) -> int:
        """
        Get group number for a pair index.
        
        PRIORITY: Use stored group_id if pair exists (handles bearish expansion correctly).
        FALLBACK: Calculate from index for legacy pairs or missing entries.
        """
        # First, check if pair exists and has explicit group_id
        pair = self.pairs.get(pair_idx)
        if pair is not None:
            return pair.group_id
        
        # Fallback: Calculate from index (legacy behavior)
        if pair_idx >= 0:
            return pair_idx // self.GROUP_OFFSET
        
    
    def _get_pair_offset(self, group_id: int) -> int:
        """Get the base pair offset for a group. Group 0 → 0, Group 1 → 100."""
        return group_id * self.GROUP_OFFSET
    
    def _find_incomplete_pair(self) -> Optional[int]:
        """
        Find the incomplete pair (has exactly ONE leg filled, not both).
        
        Incomplete pair: buy_filled XOR sell_filled = True
        - Only buy filled: buy_filled=True, sell_filled=False → incomplete
        - Only sell filled: buy_filled=False, sell_filled=True → incomplete
        - Both filled: buy_filled=True, sell_filled=True → COMPLETE
        - Neither filled: buy_filled=False, sell_filled=False → empty (not incomplete)
        
        Returns pair index or None if no incomplete pairs exist.
        """
        for idx, pair in self.pairs.items():
            # XOR: exactly one leg filled (not both, not neither)
            is_incomplete = pair.buy_filled != pair.sell_filled
            if is_incomplete:
                return idx
        return None
    
    def _is_pair_incomplete(self, pair_idx: int) -> bool:
        """Check if a specific pair is incomplete (exactly one leg filled)."""
        pair = self.pairs.get(pair_idx)
        if not pair:
            return False
        return pair.buy_filled != pair.sell_filled

    def _update_tp_sl_touch_flags(self, ask: float, bid: float):
        """
        DETERMINISTIC TP/SL DETECTION: Latch touch flags based on real quote prices.
        
        Called on every tick to detect when TP/SL levels are crossed.
        This removes timing sensitivity - we record the crossing when it happens,
        not when we later notice the position disappeared.
        """
        for ticket, info in list(self.ticket_map.items()):
            if not info or len(info) < 5:
                continue

            _, leg, _, tp_price, sl_price = info

            flags = self.ticket_touch_flags.get(ticket)
            if flags is None:
                flags = {"tp_touched": False, "sl_touched": False}
                self.ticket_touch_flags[ticket] = flags
                
            if leg == 'B':  # BUY position
                # BUY TP hit when bid >= tp_price
                if not flags['tp_touched'] and bid >= tp_price:
                    flags['tp_touched'] = True
                
                # BUY SL hit when bid <= sl_price
                if not flags['sl_touched'] and bid <= sl_price:
                    flags['sl_touched'] = True
            
            else:  # SELL position
                # SELL TP hit when ask <= tp_price
                if not flags['tp_touched'] and ask <= tp_price:
                    flags['tp_touched'] = True
                
                # SELL SL hit when ask >= sl_price
                if not flags['sl_touched'] and ask >= sl_price:
                    flags['sl_touched'] = True
    
    def _update_c_highwater(self, group_id: int, current_c: int):
        """
        Update the high-water mark for C in a group.
        Only updates if current_c is greater than the previous high-water mark.
        """
        prev = self.group_c_highwater[group_id]  # Defaultdict returns 0 if missing
        if current_c > prev:
            self.group_c_highwater[group_id] = current_c
            #print(f"[C-HIGHWATER] Group {group_id}: High-water updated {prev} -> {current_c}")
            
    def _get_c_highwater(self, group_id: int) -> int:
        """Get the high-water mark for C for expansion gating."""
        return self.group_c_highwater[group_id]

    def _count_completed_pairs_for_group(self, group_id: int) -> int:
        """Count completed pairs (C) for a specific group only."""
        offset = self._get_pair_offset(group_id)
        
        # Use MT5 authoritative source via ticket_map
        positions = mt5.positions_get(symbol=self.symbol)
        pair_legs = defaultdict(set)
        
        # 1. Map all open legs to pairs
        if positions:
            for pos in positions:
                info = self.ticket_map.get(pos.ticket)
                if info and len(info) >= 5:
                    pair_idx, leg, _, _, _ = info
                    pair_legs[pair_idx].add(leg)
        
        # 2. Count pairs with both legs that belong to this group
        live_count = 0
        for p_idx, legs in pair_legs.items():
            if legs == {'B', 'S'} and self._get_group_from_pair(p_idx) == group_id:
                live_count += 1
                
        # 3. Update High-Water Mark Logic
        self._update_c_highwater(group_id, live_count)
        
        return live_count
    
    def _is_group_locked(self, group_id: int) -> bool:
        """Check if a specific group is locked (C >= 3)."""
        return self._count_completed_pairs_for_group(group_id) >= 3
    
    async def _execute_cycle_init(self):
        """
        RENAMED: See _execute_group_init for new implementation.
        Kept for backward compatibility, routes to group init.
        """
        await self._execute_group_init(self.current_group, self.anchor_price)
    
    async def _execute_group_init(self, group_id: int, anchor_price: float, is_bullish_source: bool = True, trigger_pair_idx: int = None):
        """
        Execute group initialization with INIT pairs and optional non-atomic completing leg.

        Args:
            group_id: The new group to initialize
            anchor_price: Price at which INIT fires
            is_bullish_source: True if triggered by BUY incomplete TP, False if SELL incomplete TP
            trigger_pair_idx: The pair from previous group that triggered INIT (for non-atomic completing leg)
        """
        # Capture Group 1 Directional Intent (legacy - now also stored per group)
        if group_id == 1:
            self.group_direction = "BULLISH" if is_bullish_source else "BEARISH"
            print(f"[GROUP_INIT] Group 1 Intent Cached: {self.group_direction}")

        # ========================================================================
        # RETRACEMENT TRACKING SETUP
        # ========================================================================
        # Store init source direction for this group
        self.group_init_source[group_id] = "BULLISH" if is_bullish_source else "BEARISH"

        # Pending retracement is OPPOSITE of init source
        # Bullish init (buy TP hit) -> expect bearish retracement (price goes down)
        # Bearish init (sell TP hit) -> expect bullish retracement (price goes up)
        self.group_pending_retracement[group_id] = "BEARISH" if is_bullish_source else "BULLISH"

        # Store anchor for calculating retracement levels
        self.group_retracement_anchor[group_id] = anchor_price

        # Reset retracement levels fired for this group
        self.group_retracement_levels_fired[group_id] = set()

        print(f"[GROUP_INIT] Group {group_id}: Init={self.group_init_source[group_id]}, "
              f"Pending Retracement={self.group_pending_retracement[group_id]}")

        # GRACEFUL STOP GUARD: Block new group creation during graceful stop
        if self.graceful_stop:
            #print(f"[GROUP_INIT] {self.symbol}: Graceful stop active, blocking new group {group_id}")
            return

        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            print(f"[GROUP_INIT] {self.symbol}: No tick data, cannot init")
            return

        #async with self.execution_lock:
        offset = self._get_pair_offset(group_id)
        b_idx = offset
        s_idx = offset + 1

        print(f"[GROUP_INIT] group={group_id} anchor={anchor_price:.2f} B{b_idx}+S{s_idx}")

        # --- Build pairs using the ANCHOR as reference (deterministic) ---
        b_price = float(anchor_price)  # was tick.ask
        pair_b = GridPair(index=b_idx, buy_price=b_price, sell_price=b_price - self.spread)
        pair_b.next_action = "buy"
        pair_b.trade_count = 0
        pair_b.group_id = group_id
        self.pairs[b_idx] = pair_b

        ticket_b = await self._execute_market_order("buy", b_price, b_idx, reason="INIT")
        if not ticket_b:
            print(f"[GROUP_INIT] B{b_idx} FAILED")
            # rollback pair object
            self.pairs.pop(b_idx, None)
            return

        pair_b.buy_filled = True
        pair_b.buy_ticket = ticket_b
        # sticky ever-opened (if field exists)
        if hasattr(pair_b, "buy_ever_opened"):
            pair_b.buy_ever_opened = True
        pair_b.advance_toggle()
        print(f"[GROUP_INIT] B{b_idx} placed, ticket={ticket_b}")

        # S(offset+1) is seeded at B price (your convention)
        s_price = b_price
        pair_s = GridPair(index=s_idx, buy_price=b_price + self.spread, sell_price=s_price)
        pair_s.next_action = "sell"
        pair_s.trade_count = 0
        pair_s.group_id = group_id
        self.pairs[s_idx] = pair_s

        ticket_s = await self._execute_market_order("sell", s_price, s_idx, reason="INIT")
        if not ticket_s:
            print(f"[GROUP_INIT] S{s_idx} FAILED -> rolling back group init")
            # rollback second pair object
            self.pairs.pop(s_idx, None)
            # close the already-open buy to avoid half-init group
            try:
                self._close_position(ticket_b)
            except Exception:
                pass
            # rollback first pair object too
            self.pairs.pop(b_idx, None)
            return

        pair_s.sell_filled = True
        pair_s.sell_ticket = ticket_s
        if hasattr(pair_s, "sell_ever_opened"):
            pair_s.sell_ever_opened = True
        pair_s.advance_toggle()
        print(f"[GROUP_INIT] S{s_idx} placed, ticket={ticket_s}")

        # --- Only now commit group tracking (atomic commit) ---
        self.current_group = group_id
        self.group_anchors[group_id] = b_price
        self.anchor_price = b_price
        self.cycle_id = group_id  # keep legacy field in sync

        # Reset step triggers for new group (all directions)
        self.step1_bullish_triggered = False
        self.step1_bearish_triggered = False
        self.step2_bullish_triggered = False
        self.step2_bearish_triggered = False
        self.step1_triggered = False
        self.step2_triggered = False

        self.center_price = b_price

        # ========================================================================
        # LOG INIT TO GROUP LOGGER
        # ========================================================================
        b_tp = b_price + self.spread  # TP is spread above entry for buy
        b_sl = b_price - self.spread  # SL is spread below entry for buy
        s_tp = s_price - self.spread  # TP is spread below entry for sell
        s_sl = s_price + self.spread  # SL is spread above entry for sell

        self.group_logger.log_init(
            group_id=group_id,
            anchor=anchor_price,
            is_bullish_source=is_bullish_source,
            b_idx=b_idx,
            s_idx=s_idx,
            b_ticket=ticket_b,
            s_ticket=ticket_s,
            b_entry=b_price,
            s_entry=s_price,
            b_tp=b_tp,
            s_tp=s_tp,
            b_sl=b_sl,
            s_sl=s_sl,
            lots=self.lot_sizes[0] if self.lot_sizes else 0.01
        )

        # ========================================================================
        # NON-ATOMIC COMPLETING LEG FOR PREVIOUS GROUP
        # ========================================================================
        # When INIT fires due to incomplete pair TP, we need to also fire
        # the completing leg for the pair that was "left behind" when C=3 was reached.
        #
        # Example: S101 incomplete hits TP -> INIT B200+S201 -> Also fire S98
        # The S98 completes the pair that was left incomplete when the previous group
        # reached C=3 and did non-atomic expansion.
        if trigger_pair_idx is not None and group_id > 0:
            prev_group_id = group_id - 1
            prev_offset = self._get_pair_offset(prev_group_id)

            # Calculate the completing pair index
            # If bullish source (buy incomplete hit TP), we need to complete with SELL
            # If bearish source (sell incomplete hit TP), we need to complete with BUY
            if is_bullish_source:
                # Bullish: The trigger pair was a BUY incomplete
                # Need to fire SELL to complete a pair that's one level down
                completing_leg = "sell"
                # The incomplete pair that needs completing is at trigger - 1
                # (because atomic would have been B(n) + S(n+1), so non-atomic was just B(n))
                completing_pair_idx = trigger_pair_idx - 1
            else:
                # Bearish: The trigger pair was a SELL incomplete
                # Need to fire BUY to complete a pair that's one level up
                completing_leg = "buy"
                # The incomplete pair that needs completing is at trigger + 1
                # (because atomic would have been S(n) + B(n-1), so non-atomic was just S(n))
                completing_pair_idx = trigger_pair_idx + 1

            # Check if this pair exists and is actually incomplete
            completing_pair = self.pairs.get(completing_pair_idx)
            if completing_pair:
                needs_completing = False
                if completing_leg == "sell" and completing_pair.buy_filled and not completing_pair.sell_filled:
                    needs_completing = True
                elif completing_leg == "buy" and completing_pair.sell_filled and not completing_pair.buy_filled:
                    needs_completing = True

                if needs_completing:
                    # Fire the non-atomic completing leg
                    completing_price = tick.bid if completing_leg == "sell" else tick.ask
                    print(f"[INIT-COMPLETE] Firing non-atomic {completing_leg.upper()[0]}{completing_pair_idx} @ {completing_price:.2f}")

                    ticket_c = await self._execute_market_order(
                        completing_leg, completing_price, completing_pair_idx, reason="INIT_COMPLETE"
                    )
                    if ticket_c:
                        if completing_leg == "sell":
                            completing_pair.sell_filled = True
                            completing_pair.sell_ticket = ticket_c
                        else:
                            completing_pair.buy_filled = True
                            completing_pair.buy_ticket = ticket_c
                        completing_pair.advance_toggle()

                        # Log to group logger
                        self.group_logger.log_non_atomic_complete(
                            group_id=prev_group_id,
                            pair_idx=completing_pair_idx,
                            leg=completing_leg.upper()[0],
                            entry=completing_price,
                            reason="INIT_COMPLETE"
                        )
                        print(f"[INIT-COMPLETE] {completing_leg.upper()[0]}{completing_pair_idx} placed, ticket={ticket_c}")

        await self.save_state()

    async def _check_step_triggers(self, ask: float, bid: float):
        """
        DYNAMIC GRID EXPANSION for ALL GROUPS.

        All groups expand normally via step triggers when price moves.
        Each group uses its own anchor price stored in group_anchors.
        
        Expands grid at each new level until C >= 3 for current group.
        Grid expands in WHICHEVER direction price moves:
        - Bullish: Complete incomplete pair with B, seed next pair with S
        - Bearish: Complete incomplete pair with S, seed next pair with B
        """
        # GRACEFUL STOP GUARD: Block step triggers (new pair creation) during graceful stop
        if self.graceful_stop:
            return

        # NOTE: Step triggers now apply to ALL groups (not just Group 0)
        # Each group expands normally from its anchor price

        D = self.spread
        T = self.tolerance

        # Only count C for current group (Use High-Water Mark for gating)
        # This prevents regression if positions close
        C = self._get_c_highwater(self.current_group)
        
        # Don't expand if current group already has 3 completed pairs
        if C >= 3:
            return
        
        # Get anchor for current group (not global anchor_price!)
        current_anchor = self.group_anchors.get(self.current_group, self.anchor_price)
        
        # Filter pairs to only current group using stored group_id
        group_pairs = {idx: pair for idx, pair in self.pairs.items()
                       if pair.group_id == self.current_group}
        
        if not group_pairs:
            return
        
        # Separate positive and negative pairs within current group
        positive_pairs = [idx for idx in group_pairs.keys() if idx > 0 or (self.current_group > 0 and idx >= self.current_group * 100)]
        negative_pairs = [idx for idx in group_pairs.keys() if idx < 0 or (self.current_group > 0 and idx < self.current_group * 100)]
        
        # For Group 1+, adjust positive/negative classification based on group offset
        if self.current_group > 0:
            offset = self._get_pair_offset(self.current_group)
            # Within Group 1+, "positive" means >= offset, "negative" means < offset
            positive_pairs = [idx for idx in group_pairs.keys() if idx >= offset]
            negative_pairs = [idx for idx in group_pairs.keys() if idx < offset]
        
        # ================================================================
        # BULLISH EXPANSION: Price moving up
        # ================================================================
        # Find the highest INCOMPLETE pair in current group (has S, no B)
        incomplete_bull_pair = None
        for idx in sorted(positive_pairs, reverse=True):  # Check from highest
            pair = group_pairs.get(idx)
            if pair and pair.sell_filled and not pair.buy_filled:
                incomplete_bull_pair = idx
                break
        
        if incomplete_bull_pair is not None:
            # For Group 1+, calculate level relative to THAT pair's sell_price
            # (which was set when the pair was seeded)
            pair = group_pairs[incomplete_bull_pair]
            bull_level = pair.buy_price  # Use the stored buy_price for this pair

            if ask >= bull_level - T:
                # [DIRECTIONAL GUARD] Bullish Expansion Restriction
                # Check if bullish expansion is allowed based on pending retracement
                pending_retracement = self.group_pending_retracement.get(self.current_group)
                init_source = self.group_init_source.get(self.current_group)

                # ALLOW bullish expansion if:
                # 1. No init source set (Group 0 initial expansion), OR
                # 2. Pending retracement is BULLISH (init was bearish, expecting bullish retracement)
                if init_source == "BULLISH" and pending_retracement != "BULLISH":
                    # Init was bullish, we expect bearish retracement, NOT bullish expansion
                    # Block bullish expansion
                    pass
                else:
                    print(f"[EXPAND-BULL] ask={ask:.2f} >= level={bull_level:.2f} (C={C}, Group={self.current_group}) -> B{incomplete_bull_pair}+S{incomplete_bull_pair+1}")
                    await self._expand_bullish(incomplete_bull_pair)
        
        # ================================================================
        # BEARISH EXPANSION: Price moving down
        # ================================================================
        # Find the lowest INCOMPLETE pair in current group (has B, no S)
        incomplete_bear_pair = None
        
        # For Group 0, check Pair 0 explicitly
        if self.current_group == 0:
            pair0 = group_pairs.get(0)
            if pair0 and pair0.buy_filled and not pair0.sell_filled:
                incomplete_bear_pair = 0
        
        if incomplete_bear_pair is None:
            # Check negative/lower pairs from highest (closest to anchor) to lowest
            for idx in sorted(negative_pairs, reverse=True):
                pair = group_pairs.get(idx)
                if pair and pair.buy_filled and not pair.sell_filled:
                    incomplete_bear_pair = idx
                    break
        
        if incomplete_bear_pair is not None:
            # Use the stored sell_price for this pair
            pair = group_pairs[incomplete_bear_pair]
            bear_level = pair.sell_price

            if bid <= bear_level + T:
                # [DIRECTIONAL GUARD] Bearish Expansion Restriction
                # Check if bearish expansion is allowed based on pending retracement
                pending_retracement = self.group_pending_retracement.get(self.current_group)
                init_source = self.group_init_source.get(self.current_group)

                # ALLOW bearish expansion if:
                # 1. No init source set (Group 0 initial expansion), OR
                # 2. Pending retracement is BEARISH (init was bullish, expecting bearish retracement)
                if init_source == "BEARISH" and pending_retracement != "BEARISH":
                    # Init was bearish, we expect bullish retracement, NOT bearish expansion
                    # Block bearish expansion
                    pass
                else:
                    print(f"[EXPAND-BEAR] bid={bid:.2f} <= level={bear_level:.2f} (C={C}, Group={self.current_group}) -> S{incomplete_bear_pair}+B{incomplete_bear_pair-1}")
                    await self._expand_bearish(incomplete_bear_pair)
    
    async def _expand_bullish(self, pair_to_complete: int):
        """Expand grid bullish: complete pair N with B, start pair N+1 with S.
        If C==2, do NON-ATOMIC completion then immediately artificial-close + INIT next group.
        """
        async with self.execution_lock:
            # Use High-Water C for gating
            C = self._get_c_highwater(self.current_group)
            if C >= 3:
                print(f"[EXPAND-BULL] BLOCKED C={C} >= 3")
                return

            # [DIRECTIONAL GUARD] Bullish Expansion Restriction
            # Use per-group tracking for direction guards
            init_source = self.group_init_source.get(self.current_group)
            pending_retracement = self.group_pending_retracement.get(self.current_group)

            # Block bullish expansion if init was bullish and we're not expecting bullish retracement
            if init_source == "BULLISH" and pending_retracement != "BULLISH":
                # print(f"[GUARD] Blocking Bullish expansion (Init was BULLISH, expecting BEARISH retracement)")
                return

            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                return

            pair = self.pairs.get(pair_to_complete)
            if not pair:
                return

            # Complete with B(pair_to_complete)
            if not pair.buy_filled:
                ticket = await self._execute_market_order("buy", pair.buy_price, pair_to_complete, reason="EXPAND")
                if ticket:
                    pair.buy_filled = True
                    pair.buy_ticket = ticket
                    # sticky ever-opened (if present)
                    if hasattr(pair, "buy_ever_opened"):
                        pair.buy_ever_opened = True
                    pair.advance_toggle()
                else:
                    return  # completion failed

            # NON-ATOMIC at C==2: completing this makes C==3
            # DIRECT SOLUTION: Just fill the leg. Do NOT force Init.
            if C == 2:
                print(f"[NON-ATOMIC] C was 2, now 3 after B{pair_to_complete}. Filling leg only. Waiting for Incomplete TP to drive Init.")
                
                # Log non-atomic expansion
                self.group_logger.log_expansion(
                    group_id=self.current_group,
                    expansion_type="STEP_EXPAND",
                    pair_idx=pair_to_complete,
                    trade_type="BUY",
                    entry=pair.buy_price,
                    tp=pair.buy_price + self.spread,
                    sl=pair.buy_price - self.spread,
                    lots=self.lot_sizes[0] if self.lot_sizes else 0.01,
                    ticket=pair.buy_ticket,
                    is_atomic=False,
                    c_count=3
                )
                return

            # Otherwise seed next incomplete: S(pair_to_complete + 1)
            new_pair_idx = pair_to_complete + 1

            if new_pair_idx in self.pairs:
                print(f"[EXPAND-BULL] Seed Pair {new_pair_idx} already exists - Skipping")
                return

            new_sell_price = pair.buy_price
            new_buy_price = new_sell_price + self.spread

            new_pair = GridPair(index=new_pair_idx, buy_price=new_buy_price, sell_price=new_sell_price)
            new_pair.next_action = "sell"
            new_pair.group_id = self.current_group
            self.pairs[new_pair_idx] = new_pair

            ticket = await self._execute_market_order("sell", new_pair.sell_price, new_pair_idx, reason="EXPAND")
            if ticket:
                new_pair.sell_filled = True
                new_pair.sell_ticket = ticket
                if hasattr(new_pair, "sell_ever_opened"):
                    new_pair.sell_ever_opened = True
                new_pair.advance_toggle()

                # Log atomic expansion
                self.group_logger.log_expansion(
                    group_id=self.current_group,
                    expansion_type="STEP_EXPAND",
                    pair_idx=pair_to_complete,
                    trade_type="BUY",
                    entry=pair.buy_price,
                    tp=pair.buy_price + self.spread,
                    sl=pair.buy_price - self.spread,
                    lots=self.lot_sizes[0] if self.lot_sizes else 0.01,
                    ticket=pair.buy_ticket,
                    seed_idx=new_pair_idx,
                    seed_type="SELL",
                    seed_entry=new_pair.sell_price,
                    seed_tp=new_pair.sell_price - self.spread,
                    seed_sl=new_pair.sell_price + self.spread,
                    seed_ticket=ticket,
                    is_atomic=True,
                    c_count=C + 1
                )

    async def _expand_bearish(self, pair_to_complete: int):
        """Expand grid bearish: complete pair N with S, start pair N-1 with B.
        If C==2, do NON-ATOMIC completion then immediately artificial-close + INIT next group.
        """
        async with self.execution_lock:
            # Use High-Water C for gating
            C = self._get_c_highwater(self.current_group)
            if C >= 3:
                print(f"[EXPAND-BEAR] BLOCKED C={C} >= 3")
                return

            # [DIRECTIONAL GUARD] Bearish Expansion Restriction
            # Use per-group tracking for direction guards
            init_source = self.group_init_source.get(self.current_group)
            pending_retracement = self.group_pending_retracement.get(self.current_group)

            # Block bearish expansion if init was bearish and we're not expecting bearish retracement
            if init_source == "BEARISH" and pending_retracement != "BEARISH":
                # print(f"[GUARD] Blocking Bearish expansion (Init was BEARISH, expecting BULLISH retracement)")
                return

            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                return

            pair = self.pairs.get(pair_to_complete)
            if not pair:
                return

            # Complete with S(pair_to_to_complete)
            if not pair.sell_filled:
                ticket = await self._execute_market_order("sell", pair.sell_price, pair_to_complete, reason="EXPAND")
                if ticket:
                    pair.sell_filled = True
                    pair.sell_ticket = ticket
                    if hasattr(pair, "sell_ever_opened"):
                        pair.sell_ever_opened = True
                    pair.advance_toggle()
                else:
                    return  # completion failed

            # NON-ATOMIC at C==2: completing this makes C==3
            # DIRECT SOLUTION: Just fill the leg. Do NOT force Init.
            if C == 2:
                print(f"[NON-ATOMIC] C was 2, now 3 after S{pair_to_complete}. Filling leg only. Waiting for Incomplete TP to drive Init.")
                
                # Log non-atomic expansion
                self.group_logger.log_expansion(
                    group_id=self.current_group,
                    expansion_type="STEP_EXPAND",
                    pair_idx=pair_to_complete,
                    trade_type="SELL",
                    entry=pair.sell_price,
                    tp=pair.sell_price - self.spread,
                    sl=pair.sell_price + self.spread,
                    lots=self.lot_sizes[0] if self.lot_sizes else 0.01,
                    ticket=pair.sell_ticket,
                    is_atomic=False,
                    c_count=3
                )
                return

            # Otherwise seed next incomplete: B(pair_to_complete - 1)
            new_pair_idx = pair_to_complete - 1

            if new_pair_idx in self.pairs:
                print(f"[EXPAND-BEAR] Seed Pair {new_pair_idx} already exists - Skipping")
                return

            new_buy_price = pair.sell_price
            new_sell_price = new_buy_price - self.spread

            new_pair = GridPair(index=new_pair_idx, buy_price=new_buy_price, sell_price=new_sell_price)
            new_pair.next_action = "buy"
            new_pair.group_id = self.current_group
            self.pairs[new_pair_idx] = new_pair

            ticket = await self._execute_market_order("buy", new_pair.buy_price, new_pair_idx, reason="EXPAND")
            if ticket:
                new_pair.buy_filled = True
                new_pair.buy_ticket = ticket
                if hasattr(new_pair, "buy_ever_opened"):
                    new_pair.buy_ever_opened = True
                new_pair.advance_toggle()

                # Log atomic expansion
                self.group_logger.log_expansion(
                    group_id=self.current_group,
                    expansion_type="STEP_EXPAND",
                    pair_idx=pair_to_complete,
                    trade_type="SELL",
                    entry=pair.sell_price,
                    tp=pair.sell_price - self.spread,
                    sl=pair.sell_price + self.spread,
                    lots=self.lot_sizes[0] if self.lot_sizes else 0.01,
                    ticket=pair.sell_ticket,
                    seed_idx=new_pair_idx,
                    seed_type="BUY",
                    seed_entry=new_pair.buy_price,
                    seed_tp=new_pair.buy_price + self.spread,
                    seed_sl=new_pair.buy_price - self.spread,
                    seed_ticket=ticket,
                    is_atomic=True,
                    c_count=C + 1
                )

    
    async def _execute_step1_bullish(self):
        """Step 1 Bullish: Place B1 + S2 atomically."""
        # B1 completes Pair 1 (already has S1 from INIT)
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return
        
        pair1 = self.pairs.get(1)
        if pair1 and not pair1.buy_filled:
            ticket = await self._execute_market_order("buy", pair1.buy_price, 1, reason="STEP1")
            if ticket:
                pair1.buy_filled = True
                pair1.buy_ticket = ticket
                pair1.advance_toggle()
        
        # S2: Create Pair 2 with sell
        pair2 = GridPair(index=2, buy_price=self.anchor_price + 2*self.spread, 
                         sell_price=self.anchor_price + self.spread)
        pair2.next_action = "sell"
        pair2.group_id = self.current_group  # Track group membership
        self.pairs[2] = pair2
        
        ticket = await self._execute_market_order("sell", pair2.sell_price, 2, reason="STEP1")
        if ticket:
            pair2.sell_filled = True
            pair2.sell_ticket = ticket
            pair2.advance_toggle()
    
    async def _execute_step1_single_leg_bullish(self):
        """Step 1 Bullish (C==2): Place B1 ONLY to complete Pair 1, no S2."""
        pair1 = self.pairs.get(1)
        if pair1 and not pair1.buy_filled:
            ticket = await self._execute_market_order("buy", pair1.buy_price, 1, reason="STEP1")
            if ticket:
                pair1.buy_filled = True
                pair1.buy_ticket = ticket
                pair1.advance_toggle() # S2 skipped, Advanced toggle incremenents the trade count but does not execute a trade ie B1, so it won't fire
                print(f"[STEP1_SINGLE] B1 placed, S2 skipped (C==2)")


    
    async def _execute_step1_bearish(self):
        """Step 1 Bearish: Place S0 + B-1 atomically.
        
        S0 completes Pair 0 (already has B0 from INIT)
        B-1 starts Pair -1
        """
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return
        
        # S0: Complete Pair 0 (Pair 0 already has B0 from INIT)
        pair0 = self.pairs.get(0)
        if pair0 and not pair0.sell_filled:
            ticket = await self._execute_market_order("sell", pair0.sell_price, 0, reason="STEP1")
            if ticket:
                pair0.sell_filled = True
                pair0.sell_ticket = ticket
                pair0.advance_toggle()
        
        # B-1: Start Pair -1 (buy only)
        pair_neg1 = GridPair(index=-1, buy_price=self.anchor_price - self.spread, 
                             sell_price=self.anchor_price - 2*self.spread)
        pair_neg1.next_action = "buy"
        pair_neg1.group_id = self.current_group  # Track group membership
        self.pairs[-1] = pair_neg1
        
        ticket = await self._execute_market_order("buy", pair_neg1.buy_price, -1, reason="STEP1")
        if ticket:
            pair_neg1.buy_filled = True
            pair_neg1.buy_ticket = ticket
            pair_neg1.advance_toggle()
    
    async def _execute_step1_single_leg_bearish(self):
        """Step 1 Bearish (C==2): Place S0 ONLY to complete Pair 0, no B-1."""
        # S0: Complete Pair 0 (Pair 0 already has B0 from INIT)
        pair0 = self.pairs.get(0)
        if pair0 and not pair0.sell_filled:
            ticket = await self._execute_market_order("sell", pair0.sell_price, 0, reason="STEP1")
            if ticket:
                pair0.sell_filled = True
                pair0.sell_ticket = ticket
                pair0.advance_toggle()
                print(f"[STEP1_SINGLE] S0 placed, B-1 skipped (C==2)")
    
    async def _execute_step2_bullish(self):
        """Step 2 Bullish: Place B2 + S3 atomically."""
        pair2 = self.pairs.get(2)
        if pair2 and not pair2.buy_filled:
            ticket = await self._execute_market_order("buy", pair2.buy_price, 2, reason="STEP2")
            if ticket:
                pair2.buy_filled = True
                pair2.buy_ticket = ticket
                pair2.advance_toggle()
        
        # S3
        pair3 = GridPair(index=3, buy_price=self.anchor_price + 3*self.spread,
                         sell_price=self.anchor_price + 2*self.spread)
        pair3.next_action = "sell"
        pair3.group_id = self.current_group  # Track group membership
        self.pairs[3] = pair3
        
        ticket = await self._execute_market_order("sell", pair3.sell_price, 3, reason="STEP2")
        if ticket:
            pair3.sell_filled = True
            pair3.sell_ticket = ticket
            pair3.advance_toggle()
    
    async def _execute_step2_single_leg_bullish(self):
        """Step 2 Bullish (C >= 2): Place B2 ONLY, no S3."""
        pair2 = self.pairs.get(2)
        if pair2 and not pair2.buy_filled:
            ticket = await self._execute_market_order("buy", pair2.buy_price, 2, reason="STEP2")
            if ticket:
                pair2.buy_filled = True
                pair2.buy_ticket = ticket
                pair2.advance_toggle()
                print(f"[STEP2_SINGLE] B2 placed, S3 skipped (C >= 2)")
    
    async def _execute_step2_bearish(self):
        """Step 2 Bearish: Place S-1 + B-2 atomically.
        
        S-1 completes Pair -1 (already has B-1 from Step 1)
        B-2 starts Pair -2
        """
        # S-1: Complete Pair -1 (Pair -1 already has B-1 from Step 1)
        pair_neg1 = self.pairs.get(-1)
        if pair_neg1 and not pair_neg1.sell_filled:
            ticket = await self._execute_market_order("sell", pair_neg1.sell_price, -1, reason="STEP2")
            if ticket:
                pair_neg1.sell_filled = True
                pair_neg1.sell_ticket = ticket
                pair_neg1.advance_toggle()
        
        # B-2: Start Pair -2 (buy only)
        pair_neg2 = GridPair(index=-2, buy_price=self.anchor_price - 2*self.spread,
                             sell_price=self.anchor_price - 3*self.spread)
        pair_neg2.next_action = "buy"
        pair_neg2.group_id = self.current_group  # Track group membership
        self.pairs[-2] = pair_neg2
        
        ticket = await self._execute_market_order("buy", pair_neg2.buy_price, -2, reason="STEP2")
        if ticket:
            pair_neg2.buy_filled = True
            pair_neg2.buy_ticket = ticket
            pair_neg2.advance_toggle()
    
    async def _execute_step2_single_leg_bearish(self):
        """Step 2 Bearish (C == 2): Place S-2 ONLY to complete Pair -2, no B-3."""
        # S-2: Complete Pair -2 (Pair -2 already has B-2 from Step 2 full)
        pair_neg2 = self.pairs.get(-2)
        if pair_neg2 and not pair_neg2.sell_filled:
            ticket = await self._execute_market_order("sell", pair_neg2.sell_price, -2, reason="STEP2")
            if ticket:
                pair_neg2.sell_filled = True
                pair_neg2.sell_ticket = ticket
                pair_neg2.advance_toggle()
                print(f"[STEP2_SINGLE] S-2 placed, B-3 skipped (C == 2)")
    
    async def _enforce_hedge_invariants_gated(self):
        """
        Enforce hedge rules for COMPLETED pairs only.
        A pair is completed when both B and S positions exist.
        """
        for idx, pair in self.pairs.items():
            # GATE: Only manage hedges for COMPLETED pairs
            if not self._is_pair_completed(idx):
                continue
            
            # Check if pair is at max positions and needs hedge
            if pair.trade_count >= self.max_positions and not pair.hedge_active:
                if self.hedge_enabled:
                    # Place hedge
                    await self._place_hedge(idx, pair)
    
    async def _place_hedge(self, pair_idx: int, pair):
        """Place hedge for a maxed-out pair."""
        # Determine hedge direction (opposite of last trade)
        if pair.next_action == "buy":
            hedge_direction = "sell"  # Last was buy, hedge with sell
        else:
            hedge_direction = "buy"  # Last was sell, hedge with buy
        
        print(f"[HEDGE] Placing {hedge_direction} hedge for Pair {pair_idx}")
        # Hedge placement logic would go here (using existing _execute_hedge method if available)
    
    # ========================================================================
    # LIFECYCLE
    # ========================================================================
    
    async def start_ticker(self):
        """Called when config updates."""
        print(f" {self.symbol}: Config Updated.")
        # Could trigger re-validation of grid if spread changed significantly
        pass
    
    async def start(self):
        # FIX: Don't set self.running = True yet - wait until fully initialized
        # This prevents race conditions where ticks arrive before DB is ready
        self.start_time = time.time()
        
        # FRESH SESSION: Delete stale DB before init
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
                print(f"[FRESH] {self.symbol}: Deleted stale DB")
            except Exception as e:
                print(f"[FRESH] {self.symbol}: Could not delete DB: {e}")
        
        # Initialize Repo
        await self.repository.initialize()
        
        if not mt5.symbol_select(self.symbol, True):
            print(f" {self.symbol}: Failed to select symbol in MT5.")
            return
        
        # Load state from DB (includes cycle state)
        await self.load_state()
        
        # Load ticket map for TP detection recovery
        self.ticket_map = await self.repository.get_ticket_map()
        print(f"[START] {self.symbol}: Loaded {len(self.ticket_map)} ticket mappings")
        
        # If no state loaded (pairs empty), ensure fresh start
        if not self.pairs:
            self.phase = self.PHASE_INIT
            self.center_price = 0.0
            self.iteration = 1
            
            # FRESH START: Set cycle_id=0, anchor=current price
            self.cycle_id = 0
            tick = mt5.symbol_info_tick(self.symbol)
            self.anchor_price = tick.ask if tick else 0.0
            self.step1_triggered = False
            self.step2_triggered = False
            
            # Clear any stale ticket mappings
            await self.repository.clear_ticket_map()
            self.ticket_map = {}
            
            print(f"[FRESH] {self.symbol}: cycle_id=0 anchor={self.anchor_price:.2f}")
        else:
            # RECOVERY: cycle_id and anchor_price already loaded in load_state()
            print(f"[RECOVERY] {self.symbol}: cycle_id={self.cycle_id} anchor={self.anchor_price:.2f} pairs={len(self.pairs)}")
        
        # FIX: Only enable tick processing AFTER everything is initialized
        # This is the last line to prevent race conditions
        self.running = True
    
    async def stop(self):
        """
        Graceful stop - sets flag to complete open pairs to max_positions before stopping.
        """
        print(f"[STOP] {self.symbol}: Graceful stop initiated. Completing open pairs...")
        self.graceful_stop = True
        # Don't set self.running = False here; let _check_graceful_stop_complete handle it
        await self.save_state()
    
    async def shutdown(self):
        """
        Hard shutdown - close DB connection and delete file.
        Call this on terminate or exit.
        """
        print(f"[SHUTDOWN] {self.symbol}: Closing DB and cleaning up...")
        self.running = False
        try:
            await self.repository.close()
        except Exception as e:
            print(f"[SHUTDOWN] {self.symbol}: Error closing DB: {e}")
        
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
                print(f"[SHUTDOWN] {self.symbol}: Removed DB file")
            except Exception as e:
                print(f"[SHUTDOWN] {self.symbol}: Could not remove DB: {e}")
    
    async def _check_graceful_stop_complete(self) -> bool:
        """
        Check if graceful stop is complete (all open pairs at max_positions or hedged).
        Returns True if we should fully stop now.
        """
        if not self.graceful_stop:
            return False
        
        # Check each pair that has any trades
        for idx, pair in self.pairs.items():
            # If this pair has any active positions (buy or sell filled)
            if pair.buy_filled or pair.sell_filled:
                
                # WAIT FOR HEDGE: If hedge is active, wait for it to resolve
                if pair.hedge_active or pair.hedge_ticket > 0:
                    return False
                
                # WAIT FOR MAX POSITIONS: If not hedged, wait for max trades
                if pair.trade_count < self.max_positions:
                    # Still has trades to complete
                    return False
        
        # All active pairs have reached max_positions or completed - fully stop now
        self.running = False
        self.graceful_stop = False
        print(f"[STOP] {self.symbol}: Graceful stop complete. All pairs at max_positions/hedged.")
        await self.save_state()
        return True
    
    async def terminate(self):
        """
        Nuclear reset - close ALL positions for this symbol immediately.
        Resets all pair states and lot counters.
        """
        print(f"[TERMINATE] {self.symbol}: Closing ALL positions immediately...")
        
        # Close all open positions for this symbol
        positions = mt5.positions_get(symbol=self.symbol)
        closed_count = 0
        if positions:
            for pos in positions:
                # Pass the ticket number (int), not the position object
                if self._close_position(pos.ticket):
                    closed_count += 1
                else:
                    print(f"[ERROR] Failed to close position {pos.ticket}")
            print(f"[TERMINATE] {self.symbol}: Closed {closed_count}/{len(positions)} positions.")
        
        # Reset all pairs
        for idx, pair in self.pairs.items():
            pair.buy_filled = False
            pair.sell_filled = False
            pair.buy_ticket = 0
            pair.sell_ticket = 0
            pair.trade_count = 0
            pair.buy_in_zone = False
            pair.sell_in_zone = False
        
        # Stop the strategy
        self.running = False
        self.phase = self.PHASE_INIT
        self.pairs = {}
        self.center_price = 0.0
        
        print(f"[TERMINATE] {self.symbol}: Grid reset complete.")

    # ========================================================================
    # MAIN TICK HANDLER
    # ========================================================================
    
    async def on_external_tick(self, tick_data: Dict):
        if not self.running:
            return
        if self.is_busy:
            return
        
        # Check if graceful stop is complete
        if self.graceful_stop and await self._check_graceful_stop_complete():
            return
            
        ask = float(tick_data['ask'])
        bid = float(tick_data['bid'])
        self.current_price = ask
        self.open_positions_count = tick_data.get('positions_count', 0)
        
        try:
            self.is_busy = True
            
            # State Machine
            if self.phase == self.PHASE_INIT:
                await self._handle_init(ask, bid)
                
            elif self.phase == self.PHASE_WAITING_CENTER:
                await self._handle_waiting_center(ask, bid)
                
            elif self.phase == self.PHASE_EXPANDING:
                await self._handle_expanding(ask, bid)
                
            elif self.phase == self.PHASE_RUNNING:
                await self._handle_running(ask, bid)
                
        finally:
            self.is_busy = False
    
    # ========================================================================
    # PHASE HANDLERS
    # ========================================================================
    
    async def _handle_init(self, ask: float, bid: float):
        """
        INIT: Rigid State Machine for Atomic Startup.
        Step 0: Execute B0 -> Step 1
        Step 1: Execute S1 -> Step 2
        Step 2: Transition to EXPANDING
        Blocking execution_lock prevents race conditions.
        """
        async with self.execution_lock:
            # Re-check phase inside lock
            if self.phase != "INIT":
                return

            if self.init_step == 0:
                # --- STEP 0: INITIAL BUY (B0) ---
                # Check memory first
                if 0 in self.pairs:
                    print(f" {self.symbol}: [INIT] Pair 0 found in memory. Advancing step.")
                    self.init_step = 1
                else:
                    # Check MT5 for recovery
                    positions = mt5.positions_get(symbol=self.symbol)
                    b0_pos = next((p for p in positions if p.magic == 50000), None) if positions else None
                    
                    if b0_pos:
                        print(f" {self.symbol}: [INIT] Found B0 in MT5. recovering state.")
                        if 0 not in self.pairs:
                            self._recover_pair_from_position(0, b0_pos)
                        self.init_step = 1
                    else:
                        # Validate spread/price before entry? (Optional)
                        b0_price = ask
                        print(f" {self.symbol}: [INIT] Executing B0 @ {b0_price:.5f}")
                        
                        # Execute B0
                        self.center_price = b0_price
                        pair0 = GridPair(index=0, buy_price=b0_price, sell_price=b0_price - self.spread)
                        pair0.group_id = self.current_group  # Track group membership
                        self.pairs[0] = pair0
                        
                        ticket = await self._execute_market_order("buy", b0_price, 0)
                        if ticket:
                            pair0.buy_filled = True
                            pair0.buy_ticket = ticket
                            pair0.buy_in_zone = True
                            pair0.advance_toggle() # Advance to 'sell'
                            
                            # Place S0 pending stop immediately? No, logic says S1 is next logic step.
                            # But we usually place the Sell Stop for B0 here too.
                            pair0.sell_pending_ticket = self._place_pending_order("sell_stop", pair0.sell_price, 0)
                            
                            self.init_step = 1
                            print(f" {self.symbol}: [INIT] B0 Complete. Step 0 -> 1")
                        else:
                            print(f" {self.symbol}: [INIT] B0 Failed. Retrying next tick.")
                            del self.pairs[0]
                            return

            if self.init_step == 1:
                # --- STEP 1: INITIAL SELL (S1) ---
                # Check memory first
                if 1 in self.pairs and self.pairs[1].sell_filled:
                     print(f" {self.symbol}: [INIT] Pair 1 (S1) found in memory. Advancing step.")
                     self.init_step = 2
                else:
                    # Check MT5
                    positions = mt5.positions_get(symbol=self.symbol)
                    s1_exists = False
                    if positions:
                        # Magic 50001 = Pair 1, Sell
                        s1_pos = [p for p in positions if p.magic == 50001 and p.type == mt5.ORDER_TYPE_SELL]
                        if s1_pos:
                            s1_exists = True
                            if 1 not in self.pairs:
                                # Recover Pair 1
                                self._recover_pair_from_position(1, s1_pos[0])
                            self.init_step = 2

                    if not s1_exists:
                        # Execute S1
                        # S1 Price is typically B0 + Spread (Sell Limit location) 
                        # OR if we want 'Locked Step', maybe we open S1 at B0's Sell Price?
                        # Grid Logic: B0=Center. S1 logic usually triggers when price rises.
                        # BUT "init" implies forcing the grid structure.
                        # Standard interpretation: "B0 and S1" usually means "B0 and S0 (Companion)" or "B0 and B1/S1 pair".
                        # Given previous code "s1_price = pair0.buy_price" -> This implies S1 is actually S0 (the sell side of pair 0)?
                        # NO, Magic 50001 implies Pair 1.
                        # Let's stick to: Pair 1 Sell is at (B0 + Spread).
                        
                        pair0 = self.pairs.get(0)
                        if not pair0: return 
                        
                        # Calculate Pair 1 Levels
                        p1_buy_price = pair0.buy_price + self.spread
                        p1_sell_price = pair0.buy_price # Pair 1 Sell is at B0 Price? 
                        # Wait, Grid:
                        # Level 0: Buy @ X, Sell @ X-spread
                        # Level 1: Buy @ X+spread, Sell @ X
                        
                        p1_sell_target = pair0.buy_price # Effectively Center Price
                        
                        # Check price condition? 
                        # If we just force open S1 at market, it might be far off if price hasn't moved.
                        # "Strict Atomic Execution" usually implies verifying they EXIST.
                        # If price is not there, we should place a LIMIT/STOP order?
                        # The user says "Execute S1".
                        # Let's place it as PENDING if not valid for market?
                        # Actually, previous code tried to execute MARKET order implies it expects price to be there OR forces it.
                        # Let's try to place a PENDING order for S1 if Market is not valid, OR just wait.
                        # But Constraint says "If self.init_step == 1, Execute S1 -> Set init_step = 2".
                        # If we place a pending order, is that "Executed"? Yes, it establishes the grid.
                        
                        print(f" {self.symbol}: [INIT] Establishing S1 (Pair 1).")
                        pair1 = GridPair(index=1, buy_price=p1_buy_price, sell_price=p1_sell_target)
                        # FIX: Positive pairs start with SELL, so set next_action="sell"
                        # After advance_toggle(), it will correctly become "buy"
                        pair1.next_action = "sell"
                        pair1.group_id = self.current_group  # Track group membership
                        self.pairs[1] = pair1
                        
                        # For INIT, we typically want the grid ACTIVE.
                        # If we place a pending SELL LIMIT at p1_sell_target (which is B0 price), it will fill if price > B0.
                        # If price is at B0, Sell Limit @ B0 fills immediately? No, Limit Sell is "price >= target".
                        # If current price ~ B0, Sell Limit @ B0 is marketable.
                        
                        # Let's try Market Execution if close, else Pending.
                        ticket_s1 = await self._execute_market_order("sell", p1_sell_target, 1)
                        if ticket_s1:
                             pair1.sell_filled = True
                             pair1.sell_ticket = ticket_s1
                             pair1.sell_in_zone = True
                             pair1.advance_toggle()
                             pair1.buy_pending_ticket = self._place_pending_order("buy_stop", p1_buy_price, 1)
                             print(f" {self.symbol}: [INIT] S1 Filled (Market). Step 1 -> 2")
                             self.init_step = 2
                        else:
                             # Market failed (maybe distance?), place Pending
                             print(f" {self.symbol}: [INIT] S1 Market failed. Placing Pending Sell Limit.")
                             pair1.sell_pending_ticket = self._place_pending_order("sell_limit", p1_sell_target, 1)
                             pair1.buy_pending_ticket = self._place_pending_order("buy_stop", p1_buy_price, 1)
                             # We consider S1 "established" (pending or filled).
                             self.init_step = 2

            if self.init_step == 2:
                print(f" {self.symbol}: [INIT] Logic Complete. Transitioning to RUNNING.")
                self.phase = "RUNNING"  # Or EXPANDING logic
                self.last_trade_time = time.time()
            
        await self.save_state() 

    async def _handle_waiting_center(self, ask: float, bid: float):
        """
        WAITING_CENTER: Monitor for B1 and S1 fills.
        When one fills, move the other to that entry price (re-anchor).
        When BOTH have filled, transition to EXPANDING.
        """
        pair = self.pairs.get(0)
        if not pair:
            self.phase = self.PHASE_INIT
            return
        
        # Check if B1 filled (Ask reached Buy Stop price)
        if not pair.buy_filled:
            if ask >= pair.buy_price:
                # B1 triggered! Execute market buy
                ticket = await self._execute_market_order("buy", pair.buy_price, pair.index)
                if ticket:
                    pair.buy_filled = True
                    pair.buy_ticket = ticket
                    print(f" {self.symbol}: B1 FILLED @ {pair.buy_price:.2f}")
                    
                    # Re-anchor S1: Cancel old, place new at B1 - Spread
                    if not pair.sell_filled:
                        self._cancel_order(pair.sell_pending_ticket)
                        
                        # S1 new price = B1 entry - spread (to maintain spread distance)
                        new_s1_price = pair.buy_price - self.spread
                        pair.sell_price = new_s1_price
                        
                        # Since price is at B1 (high), S1 is below = Sell Stop
                        pair.sell_pending_ticket = self._place_pending_order(
                            "sell_stop", new_s1_price, pair.index
                        )
                        print(f"   S1 Re-anchored to {new_s1_price:.2f} (Sell Stop)")
                    
                    await self.save_state()
        
        # Check if S1 filled (Bid reached Sell Stop price)
        if not pair.sell_filled:
            if bid <= pair.sell_price:
                # S1 triggered! Execute market sell
                ticket = await self._execute_market_order("sell", pair.sell_price, pair.index)
                if ticket:
                    pair.sell_filled = True
                    pair.sell_ticket = ticket
                    print(f" {self.symbol}: S1 FILLED @ {pair.sell_price:.2f}")
                    
                    # Re-anchor B1: Cancel old, place new at S1 + Spread
                    if not pair.buy_filled:
                        self._cancel_order(pair.buy_pending_ticket)
                        
                        # B1 new price = S1 entry + spread
                        new_b1_price = pair.sell_price + self.spread
                        pair.buy_price = new_b1_price
                        
                        # Since price is at S1 (low), B1 is above = Buy Stop
                        pair.buy_pending_ticket = self._place_pending_order(
                            "buy_stop", new_b1_price, pair.index
                        )
                        print(f"   B1 Re-anchored to {new_b1_price:.2f} (Buy Stop)")
                    
                    await self.save_state()
        
        # Check if BOTH filled -> transition
        if pair.buy_filled and pair.sell_filled:
            self.center_price = (pair.buy_price + pair.sell_price) / 2
            self.phase = self.PHASE_EXPANDING
            print(f" {self.symbol}: Center Pair Complete. Expanding Grid...")
            await self.save_state()
            return
        
        # Check if a filled position has closed (TP/SL hit) before the other side filled
        positions = mt5.positions_get(symbol=self.symbol)
        open_tickets = set(p.ticket for p in positions) if positions else set()
        
        # If buy was filled but position is now closed, re-open it
        if pair.buy_filled and pair.buy_ticket and pair.buy_ticket not in open_tickets:
            print(f" {self.symbol}: Pair 0 Buy hit TP/SL, re-opening @ {pair.buy_price:.2f}")
            pair.buy_filled = False
            pair.buy_ticket = 0
            pair.first_fill_direction = ""  # Reset first fill tracking
            
            # [FIX] Reset trade count to 0 so next trade starts at Lot 0
            pair.trade_count = 0
            
            # Place new virtual buy trigger
            pair.buy_pending_ticket = self._place_pending_order(
                self._get_order_type("buy", pair.buy_price),
                pair.buy_price,
                0
            )
            await self.save_state()
            return
        
        # If sell was filled but position is now closed, re-open it
        if pair.sell_filled and pair.sell_ticket and pair.sell_ticket not in open_tickets:
            print(f" {self.symbol}: Pair 0 Sell hit TP/SL, re-opening @ {pair.sell_price:.2f}")
            pair.sell_filled = False
            pair.sell_ticket = 0
            
            # [FIX] Reset trade count to 0 so next trade starts at Lot 0
            pair.trade_count = 0
            
            # Place new virtual sell trigger
            pair.sell_pending_ticket = self._place_pending_order(
                self._get_order_type("sell", pair.sell_price),
                pair.sell_price,
                0
            )
            await self.save_state()
    
    async def _handle_expanding(self, ask: float, bid: float):
        """
        EXPANDING: Groups + Cap system - skip old expansion logic.
        Step triggers now handle all expansion based on anchor geometry.
        Just transition directly to RUNNING phase.
        """
        # Transition to RUNNING immediately - step triggers handle expansion
        self.phase = self.PHASE_RUNNING
        print(f" {self.symbol}: Transitioning to RUNNING. Step triggers handle expansion.")
        await self.save_state()
    
    async def _create_expansion_pair(self, index: int, reference_pair: GridPair, ask: float, bid: float):
        """
        Create a new pair relative to a reference pair.
        
        LADDER STRUCTURE:
        - For positive index (above): 
          - New SELL = Reference's BUY (they share the same price level)
          - New BUY = New SELL + spread (one spread above)
          
        - For negative index (below):
          - New BUY = Reference's SELL (they share the same price level)
          - New SELL = New BUY - spread (one spread below)
          
        This ensures continuous price levels across the grid.
        """
        if index > 0:
            # POSITIVE GRID (above reference)
            # New pair's SELL shares price level with reference's BUY
            sell_price = reference_pair.buy_price
            buy_price = sell_price + self.spread
            
            pair = GridPair(
                index=index,
                buy_price=buy_price,
                sell_price=sell_price
            )
            # POSITIVE pairs: SELL triggers first
            pair.next_action = "sell"
            
            # Above current price: Sell Limit (triggers first), Buy Stop
            pair.sell_pending_ticket = self._place_pending_order("sell_limit", sell_price, index)
            pair.buy_pending_ticket = self._place_pending_order("buy_stop", buy_price, index)
            
            print(f" {self.symbol}: Pair {index} Created (ABOVE). S@{sell_price:.2f} B@{buy_price:.2f} [next=SELL]")
        else:
            # NEGATIVE GRID (below reference)
            # New pair's BUY shares price level with reference's SELL
            buy_price = reference_pair.sell_price
            sell_price = buy_price - self.spread
            
            pair = GridPair(
                index=index,
                buy_price=buy_price,
                sell_price=sell_price
            )
            # NEGATIVE pairs: BUY triggers first
            pair.next_action = "buy"
            
            # Below current price: Buy Limit (triggers first), Sell Stop
            pair.buy_pending_ticket = self._place_pending_order("buy_limit", buy_price, index)
            pair.sell_pending_ticket = self._place_pending_order("sell_stop", sell_price, index)
            
            print(f" {self.symbol}: Pair {index} Created (BELOW). B@{buy_price:.2f} S@{sell_price:.2f} [next=BUY]")
        
        self.pairs[index] = pair
    
    async def _handle_running(self, ask: float, bid: float):
        """
        RUNNING: Groups + 3-Cap System.
        - Check step triggers for grid expansion
        - Monitor TP/SL for cycle rollover
        - Enforce hedge rules for COMPLETED pairs only
        """
        # ================================================================
        # [FIRST] Update TP/SL touch flags BEFORE position drop detection
        # This latches the crossing event when it happens, not when we
        # later notice the position disappeared. Critical for deterministic
        # TP/SL classification.
        # ================================================================
        try:
            self._update_tp_sl_touch_flags(ask, bid)
        except Exception as e:
            print(f"[ERROR] touch_flags: {e}")

        # Check for active positions
        positions = mt5.positions_get(symbol=self.symbol)
        active_count = len(positions) if positions else 0
        
        if active_count > 0:
            self.last_trade_time = time.time()
        
        # New cycles are triggered by TP events only.
        
        # [PRIMARY] Position drop detection for TP/SL and group rollover
        await self._check_position_drops(ask, bid)

        # ================================================================
        # [SATURATION TRIGGER] Proactive Check for C >= 3
        # RESTRICTED: Applies ONLY to Group 0 per user requirement.
        # Checks if Group 0 is done and forces rollover immediately.
        # ================================================================
        try:
            # 1. Get High-Water C for active group
            c_highwater = self._get_c_highwater(self.current_group)
            
            # 2. Check Saturation (C >= 3)
            # Use a latch to prevent repeated firing for the same group transition
            next_group = self.current_group + 1
            
            # USER RULE: "Only for group 0 should this apply"
            if self.current_group == 0 and c_highwater >= 3 and not self._is_group_init_triggered(next_group):
                print(f"[SATURATION] Group {self.current_group} reached C={c_highwater} >= 3. Forcing Artificial TP/Init (Proactive, Group 0 Special).")
                
                # Create a minimal tick object/dict if needed for the call
                tick_obj = type('Tick', (), {'ask': ask, 'bid': bid})()
                
                # FORCE THE HANDOFF
                await self._force_artificial_tp_and_init(tick_obj, event_price=(ask+bid)/2)
                
                # Mark as triggered to prevent spam
                self._mark_group_init_triggered(next_group)
                
        except Exception as e:
            print(f"[ERROR] Saturation Check: {e}")

        try:
            # [STEP TRIGGERS] Check anchor geometry triggers
            await self._check_step_triggers(ask, bid)
            # [HEDGE SUPERVISOR] Enforce hedge rules for COMPLETED pairs only
            await self._enforce_hedge_invariants_gated()
            # [TOGGLE TRIGGERS] For completed pairs: continue trading to max_positions
            # This allows completed pairs to toggle (buy→sell→buy...) until max then hedge
            await self._check_virtual_triggers(ask, bid)
        except Exception as e:
            print(f"[ERROR] post-drop logic: {e}")

    async def _update_fill_status(self):
        """Check MT5 positions and update fill status in pairs."""
        positions = mt5.positions_get(symbol=self.symbol)
        position_map = {}
        if positions:
            for pos in positions:
                idx = pos.magic - 50000  # Decode index from magic
                if idx not in position_map:
                    position_map[idx] = []
                position_map[idx].append(pos)
        
        # Update pairs based on actual positions
        for idx, pair in self.pairs.items():
            idx_positions = position_map.get(idx, [])
            
            # Check if we have a buy position for this pair
            buys = [p for p in idx_positions if p.type == mt5.ORDER_TYPE_BUY]
            if buys and not pair.buy_filled:
                pair.buy_filled = True
                pair.buy_ticket = buys[0].ticket
                pair.buy_pending_ticket = 0
            
            # Check if we have a sell position for this pair
            sells = [p for p in idx_positions if p.type == mt5.ORDER_TYPE_SELL]
            if sells and not pair.sell_filled:
                pair.sell_filled = True
                pair.sell_ticket = sells[0].ticket
                pair.sell_pending_ticket = 0
    
    async def _check_and_expand(self):
        """
        Check fills and manage grid:
        1. Reset fills when BOTH buy and sell are filled AND price is in neutral zone
        2. Chain next pair's sell when positive grid buy fills
        3. Chain next pair's buy when negative grid sell fills
        4. Expand grid if not at capacity
        """
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return
        
        for idx, pair in list(self.pairs.items()):
            # TOGGLE RESET: Only reset when both filled AND price is in neutral zone
            # Neutral zone = between sell_price and buy_price
            if pair.buy_filled and pair.sell_filled:
                # Check if price is between sell and buy (neutral zone)
                price_in_neutral = pair.sell_price < tick.bid < pair.buy_price
                
                if price_in_neutral:
                    print(f" {self.symbol}: Pair {idx} both filled + price in neutral zone - resetting")
                    pair.buy_filled = False
                    pair.sell_filled = False
                    # Re-place virtual triggers
                    pair.buy_pending_ticket = self._place_pending_order(
                        self._get_order_type("buy", pair.buy_price),
                        pair.buy_price, idx
                    )
                    pair.sell_pending_ticket = self._place_pending_order(
                        self._get_order_type("sell", pair.sell_price),
                        pair.sell_price, idx
                    )
                    await self.save_state()
                    return  # One op per tick
        
        # CHAIN SELL: When positive grid BUY fills, place next pair's SELL at same price
        if len(self.pairs) < self.max_pairs:
            current_indices = sorted(self.pairs.keys())
            
            for idx in current_indices:
                if idx > 0:  # Positive grid
                    pair = self.pairs[idx]
                    next_idx = idx + 1
                    
                    # If this pair's BUY just filled and next pair doesn't exist
                    if pair.buy_filled and next_idx not in self.pairs:
                        if len(self.pairs) < self.max_pairs:
                            # Create next pair: S_next = B_current, B_next = S_next + spread
                            new_sell = pair.buy_price  # S2 at B1's price
                            new_buy = new_sell + self.spread
                            
                            new_pair = GridPair(
                                index=next_idx,
                                buy_price=new_buy,
                                sell_price=new_sell
                            )
                            # Place both triggers - sell should trigger immediately
                            new_pair.sell_pending_ticket = self._place_pending_order("sell_limit", new_sell, next_idx)
                            new_pair.buy_pending_ticket = self._place_pending_order("buy_stop", new_buy, next_idx)
                            
                            # [FIX] Explicitly set start direction
                            new_pair.next_action = "sell"
                            
                            self.pairs[next_idx] = new_pair
                            
                            print(f" {self.symbol}: Chained Pair {next_idx} from B{idx}. S@{new_sell:.2f} B@{new_buy:.2f} [next=SELL]")
                            await self.save_state()
                            return
                
                elif idx < 0:  # Negative grid
                    pair = self.pairs[idx]
                    next_idx = idx - 1  # More negative
                    
                    # If this pair's SELL just filled and next pair doesn't exist
                    if pair.sell_filled and next_idx not in self.pairs:
                        if len(self.pairs) < self.max_pairs:
                            # Create next pair: B_next = S_current, S_next = B_next - spread
                            new_buy = pair.sell_price  # B-2 at S-1's price
                            new_sell = new_buy - self.spread
                            
                            new_pair = GridPair(
                                index=next_idx,
                                buy_price=new_buy,
                                sell_price=new_sell
                            )
                            # Place both triggers
                            new_pair.buy_pending_ticket = self._place_pending_order("buy_limit", new_buy, next_idx)
                            new_pair.sell_pending_ticket = self._place_pending_order("sell_stop", new_sell, next_idx)
                            
                            # [FIX] Explicitly set start direction
                            new_pair.next_action = "buy"
                            
                            self.pairs[next_idx] = new_pair
                            
                            print(f" {self.symbol}: Chained Pair {next_idx} from S{idx}. B@{new_buy:.2f} S@{new_sell:.2f} [next=BUY]")
                            await self.save_state()
                            return

    async def _monitor_position_drops(self):
        """
        TICKET LIFECYCLE VERIFICATION: Reliable drop detection using specific ticket checks.
        """
        # Iterate over copy of items to allow safe modification during loop
        for pair_idx, pair in list(self.pairs.items()):
            active_tickets = []
            if pair.buy_ticket > 0:
                active_tickets.append((pair.buy_ticket, "buy"))
            if pair.sell_ticket > 0:
                active_tickets.append((pair.sell_ticket, "sell"))
            
            if not active_tickets:
                continue
            
            for ticket_id, direction in active_tickets:
                # CHECK 1: Is it Alive?
                live_pos = mt5.positions_get(ticket=ticket_id)
                if live_pos:
                    continue
                
                # CHECK 2: Is it Closed? (Confirmed in History)
                from_time = datetime.now() - timedelta(hours=24)
                to_time = datetime.now() + timedelta(hours=1)
                history = mt5.history_deals_get(from_time, to_time, position=ticket_id)
                
                if history:
                    print(f"[DROP CONFIRMED] {self.symbol} Pair {pair_idx}: Ticket {ticket_id} ({direction}) closed.")
                    # NUCLEAR RESET DISABLED: Don't close survivor positions
                    # await self._execute_pair_reset(pair_idx, pair, direction)
                    break 
                
                # CHECK 3: Ghost/Latency (Missing from both)
                age = pair.get_position_age(ticket_id)
                if age < 3.0:
                    continue # Assume Latency
                else:
                    print(f"[GHOST DETECTED] {self.symbol} Pair {pair_idx}: Ticket {ticket_id} (age={age:.1f}s) missing.")
                    # NUCLEAR RESET DISABLED: Don't close survivor positions
                    # await self._execute_pair_reset(pair_idx, pair, direction)
                    break

    # NUCLEAR RESET DISABLED: These functions are no longer used
    # Survivor legs now stay open when opposite leg closes
    # async def _execute_pair_reset(self, pair_idx: int, pair, closed_direction: str):
    #     """Helper to execute nuclear reset."""
    #     await self._close_pair_positions(pair_idx, "both")
    #     if pair.hedge_active and pair.hedge_ticket:
    #         self._close_position(pair.hedge_ticket)
    #     
    #     # NOTE: Phoenix reset removed - pairs are no longer recycled
    #     await self.save_state()

    # async def _execute_pair_reset(self, pair_idx: int, pair, closed_direction: str):
    #     """Execute nuclear reset for a pair after confirmed position closure."""
    #     # Close any remaining positions
    #     await self._close_pair_positions(pair_idx, "both")
    #     
    #     # Close hedge if active
    #     if pair.hedge_active and pair.hedge_ticket:
    #         print(f"   [HEDGE] Closing hedge position {pair.hedge_ticket}")
    #         self._close_position(pair.hedge_ticket)
    #     
    #     # Save immediately
    #     await self.save_state()

    async def _force_artificial_tp_and_init(self, tick, event_price: float = None):
        """
        ARTIFICIAL TP: Close incomplete pair and fire INIT when rollover condition met (C=3).
        """
        # NOTE: Graceful stop check moved to END of function (block INIT only, allow cleanup)

        positions = mt5.positions_get(symbol=self.symbol)
        
        # Build map of pair_idx -> dict of leg->ticket for CURRENT GROUP
        pair_legs_map = defaultdict(dict)
        
        if positions:
            for pos in positions:
                info = self.ticket_map.get(pos.ticket)
                if info and len(info) >= 2:
                    pair_idx = info[0]
                    leg = info[1]
                    if self._get_group_from_pair(pair_idx) == self.current_group:
                        pair_legs_map[pair_idx][leg] = pos.ticket

        # Find incomplete pair (exactly 1 leg open)
        incomplete_ticket = None
        incomplete_pair_idx = None
        incomplete_leg = None
        
        for p_idx, legs_dict in pair_legs_map.items():
            if len(legs_dict) == 1:
                incomplete_pair_idx = p_idx
                # Get the ticket and leg
                incomplete_leg = list(legs_dict.keys())[0]
                incomplete_ticket = list(legs_dict.values())[0]
                break
        
        if incomplete_ticket:
            print(f"[ARTIFICIAL-TP] Closing incomplete pair {incomplete_pair_idx} ticket={incomplete_ticket}")
            self._close_position(incomplete_ticket)
            
            # Cleanup
            if incomplete_ticket in self.ticket_map:
                del self.ticket_map[incomplete_ticket]
            if incomplete_ticket in self.ticket_touch_flags:
                del self.ticket_touch_flags[incomplete_ticket]
            await self.repository.delete_ticket(incomplete_ticket)
        else:
            print(f"[ARTIFICIAL-TP] No incomplete pair found in Group {self.current_group}")

        # Fire INIT - BLOCKED during graceful stop
        if self.graceful_stop:
            print(f"[GRACEFUL-STOP] {self.symbol}: Artificial TP complete (cleanup done), BLOCKING new group INIT due to timeout.")
            return

        init_price = event_price if event_price is not None else (tick.ask + tick.bid)/2
        is_bullish_source = (incomplete_leg == 'B') if incomplete_leg else True
        print(f"[ARTIFICIAL-TP] Firing INIT for Group {self.current_group + 1} at {init_price:.2f} (Bullish={is_bullish_source})")
        await self._execute_group_init(
            self.current_group + 1, init_price,
            is_bullish_source=is_bullish_source,
            trigger_pair_idx=incomplete_pair_idx
        )

    async def _execute_tp_expansion(self, group_id: int, event_price: float, is_bullish: bool, C: int):
        """
        TP-DRIVEN ATOMIC EXPANSION for active group completed pair TP.
        """
        # GRACEFUL STOP GUARD: Block TP-driven expansion during graceful stop
        if self.graceful_stop:
            print(f"[TP-EXPAND] {self.symbol}: Graceful stop active, blocking expansion")
            return

        async with self.execution_lock:
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick: return

            # Find edge incomplete pairs for this group
            group_pairs = {idx: pair for idx, pair in self.pairs.items()
                        if self._get_group_from_pair(idx) == group_id}
            if not group_pairs: return
            sorted_indices = sorted(group_pairs.keys())

            if is_bullish:
                # Bullish: Buy leg hit TP -> Expand UP
                bullish_edge = None
                for idx in reversed(sorted_indices):
                    pair = group_pairs[idx]
                    if pair.sell_filled and not pair.buy_filled:
                        bullish_edge = pair
                        break
                
                if not bullish_edge: return
                
                complete_idx = bullish_edge.index
                seed_idx = complete_idx + 1
                
                if C == 2:
                    print(f"[TP-EXPAND] C==2: B{complete_idx} only (Non-Atomic Fill)")
                    # Fire Non-Atomic Leg ONLY
                    await self._place_single_leg_tp("buy", tick.ask, complete_idx)
                    # DO NOT Force Init. Wait for Incomplete Pair TP.
                else:
                    print(f"[TP-EXPAND] Atomic: B{complete_idx} + S{seed_idx}")
                    await self._place_atomic_bullish_tp(event_price, complete_idx, seed_idx)

                    # Log atomic TP expansion
                    pair_complete = self.pairs.get(complete_idx)
                    pair_seed = self.pairs.get(seed_idx)
                    if pair_complete and pair_seed:
                        self.group_logger.log_expansion(
                            group_id=group_id,
                            expansion_type="TP_EXPAND",
                            pair_idx=complete_idx,
                            trade_type="BUY",
                            entry=pair_complete.buy_price,
                            tp=pair_complete.buy_price + self.spread,
                            sl=pair_complete.buy_price - self.spread,
                            lots=self.lot_sizes[0] if self.lot_sizes else 0.01,
                            ticket=pair_complete.buy_ticket if pair_complete else 0,
                            seed_idx=seed_idx,
                            seed_type="SELL",
                            seed_entry=pair_seed.sell_price,
                            seed_tp=pair_seed.sell_price - self.spread,
                            seed_sl=pair_seed.sell_price + self.spread,
                            seed_ticket=pair_seed.sell_ticket if pair_seed else 0,
                            is_atomic=True,
                            c_count=C + 1
                        )
            else:
                # Bearish: Sell leg hit TP -> Expand DOWN
                bearish_edge = None
                for idx in sorted_indices:
                    pair = group_pairs[idx]
                    if pair.buy_filled and not pair.sell_filled:
                        bearish_edge = pair
                        break

                if not bearish_edge: return

                complete_idx = bearish_edge.index
                seed_idx = complete_idx - 1

                if C == 2:
                    print(f"[TP-EXPAND] C==2: S{complete_idx} only (Non-Atomic Fill)")
                    # Fire Non-Atomic Leg ONLY
                    await self._place_single_leg_tp("sell", tick.bid, complete_idx)
                    # DO NOT Force Init. Wait for Incomplete Pair TP.
                else:
                    print(f"[TP-EXPAND] Atomic: S{complete_idx} + B{seed_idx}")
                    await self._place_atomic_bearish_tp(event_price, complete_idx, seed_idx)

                    # Log atomic TP expansion
                    pair_complete = self.pairs.get(complete_idx)
                    pair_seed = self.pairs.get(seed_idx)
                    if pair_complete and pair_seed:
                        self.group_logger.log_expansion(
                            group_id=group_id,
                            expansion_type="TP_EXPAND",
                            pair_idx=complete_idx,
                            trade_type="SELL",
                            entry=pair_complete.sell_price,
                            tp=pair_complete.sell_price - self.spread,
                            sl=pair_complete.sell_price + self.spread,
                            lots=self.lot_sizes[0] if self.lot_sizes else 0.01,
                            ticket=pair_complete.sell_ticket if pair_complete else 0,
                            seed_idx=seed_idx,
                            seed_type="BUY",
                            seed_entry=pair_seed.buy_price,
                            seed_tp=pair_seed.buy_price + self.spread,
                            seed_sl=pair_seed.buy_price - self.spread,
                            seed_ticket=pair_seed.buy_ticket if pair_seed else 0,
                            is_atomic=True,
                            c_count=C + 1
                        )

    async def _place_single_leg_tp(self, direction: str, price: float, pair_idx: int):
        pair = self.pairs.get(pair_idx)
        if not pair: return 
        pair.trade_count = 1
        ticket = await self._execute_market_order(direction, price, pair_idx, reason="TP_EXPAND")
        if ticket:
            if direction == "buy":
                pair.buy_filled = True
                pair.buy_ticket = ticket
            else:
                pair.sell_filled = True
                pair.sell_ticket = ticket
            pair.advance_toggle()
            
    async def _place_atomic_bullish_tp(self, price: float, b_idx: int, s_idx: int):
        # B(n) at market
        tick = mt5.symbol_info_tick(self.symbol)
        pair_b = self.pairs.get(b_idx)
        if pair_b:
                pair_b.trade_count = 1
                ticket = await self._execute_market_order("buy", tick.ask, b_idx, reason="TP_EXPAND")
                if ticket:
                    pair_b.buy_filled = True
                    pair_b.buy_ticket = ticket
                    pair_b.advance_toggle()

        if s_idx in self.pairs:
            print(f"[TP-EXPAND] Skipping Seed S{s_idx} - Pair already exists")
            return
        
        # S(n+1) seeded at TP levels
        seed_pair = GridPair(index=s_idx, buy_price=price + self.spread, sell_price=price)
        seed_pair.next_action = "sell"
        seed_pair.trade_count = 0
        seed_pair.group_id = self.current_group
        self.pairs[s_idx] = seed_pair
        
        ticket_s = await self._execute_market_order("sell", tick.bid, s_idx, reason="TP_EXPAND")
        if ticket_s:
            seed_pair.sell_filled = True
            seed_pair.sell_ticket = ticket_s
            seed_pair.advance_toggle()

    async def _place_atomic_bearish_tp(self, price: float, s_idx: int, b_idx: int):
        # S(n) at market
        tick = mt5.symbol_info_tick(self.symbol)
        pair_s = self.pairs.get(s_idx)
        if pair_s:
                pair_s.trade_count = 1
                ticket = await self._execute_market_order("sell", tick.bid, s_idx, reason="TP_EXPAND")
                if ticket:
                    pair_s.sell_filled = True
                    pair_s.sell_ticket = ticket
                    pair_s.advance_toggle()
        if b_idx in self.pairs:
            print(f"[TP-EXPAND] Skipping Seed B{b_idx} - Pair already exists")
            return
        
        # B(n-1) seeded at TP levels
        seed_pair = GridPair(index=b_idx, buy_price=price, sell_price=price - self.spread)
        seed_pair.next_action = "buy"
        seed_pair.trade_count = 0
        seed_pair.group_id = self.current_group
        self.pairs[b_idx] = seed_pair
        
        ticket_b = await self._execute_market_order("buy", tick.ask, b_idx, reason="TP_EXPAND")
        if ticket_b:
            seed_pair.buy_filled = True
            seed_pair.buy_ticket = ticket_b
            seed_pair.advance_toggle()

    async def _handle_completed_pair_expansion(self, event_price: float, is_bullish: bool):
        """
        Handle expansion in active group driven by prior group TP.
        This simply routes the event to check expansion conditions for the ACTIVE group.
        """
        group_id = self.current_group
        C = self._count_completed_pairs_for_group(group_id)
        if C >= 3: 
             # Already full, no expansion needed
             return

        #print(f"[PRIOR-TP-DRIVER] Driving Active Group {group_id} Check (C={C})")
        # Reuse the main expansion logic
        await self._execute_tp_expansion(group_id, event_price, is_bullish, C)


    from collections import defaultdict
    from typing import Dict, Set

    async def _check_position_drops(self, ask: float, bid: float):
        """
        POSITION DROP DETECTION: Detect closed positions and classify TP/SL.

        Deterministic classification:
        - tp_touched=True -> TP, event_price = tp_price
        - sl_touched=True -> SL, event_price = sl_price
        - neither -> UNKNOWN (cleanup only)

        Authoritative completeness:
        - computed from MT5 open positions + ticket_map (NOT pair.buy_filled/sell_filled)

        IMPORTANT:
        - No direct INIT on incomplete TP here.
        - Group rollover/INIT must be handled by your C==2 non-atomic + artificial close path.
        """
        try:
            positions = mt5.positions_get(symbol=self.symbol)
            current_tickets = set(pos.ticket for pos in positions) if positions else set()

            tracked_tickets = set(self.ticket_map.keys())
            dropped_tickets = tracked_tickets - current_tickets
            if not dropped_tickets:
                return

            # Build AUTHORITATIVE "still-open legs per pair" AFTER the drop (from current MT5 positions)
            pair_legs_open: Dict[int, Set[str]] = defaultdict(set)
            for pos in (positions or []):
                info = self.ticket_map.get(pos.ticket)
                if not info or len(info) < 5:
                    continue
                p_idx, p_leg, _, _, _ = info  # (pair_idx, leg, entry, tp, sl)
                pair_legs_open[p_idx].add(p_leg)

            for ticket in dropped_tickets:
                info = self.ticket_map.get(ticket)
                if not info:
                    continue

                # Canonical tuple: (pair_idx, leg, entry_price, tp_price, sl_price)
                if len(info) < 5:
                    print(f"[DROP] Legacy info format for {ticket}, cleanup only")
                    self.ticket_map.pop(ticket, None)
                    self.ticket_touch_flags.pop(ticket, None)
                    await self.repository.delete_ticket(ticket)
                    continue

                pair_idx, leg, entry_price, tp_price, sl_price = info
                group_id = self._get_group_from_pair(pair_idx)
                is_bullish = (leg == "B")  # MUST be defined for both active/prior paths

                # Deterministic TP/SL classification from latched flags
                flags = self.ticket_touch_flags.get(ticket, {"tp_touched": False, "sl_touched": False})
                tp_touched = bool(flags.get("tp_touched", False))
                sl_touched = bool(flags.get("sl_touched", False))

                if tp_touched:
                    is_tp = True
                    event_price = tp_price
                    reason = "TP"
                elif sl_touched:
                    is_tp = False
                    event_price = sl_price
                    reason = "SL"
                else:
                    # ================================================================
                    # FALLBACK INFERENCE: Position closed between ticks before we latched flags.
                    # Compare distances. If price is WAY CLOSER to TP than SL, it's a TP.
                    # ================================================================
                    
                    if leg == 'B':  # BUY position
                        current_price = bid
                        dist_tp = abs(current_price - tp_price)
                        dist_sl = abs(current_price - sl_price)
                        
                        # Tie-breaker: If within 10% of TP distance? No, direct comparison is usually enough.
                        # But SL is usually far away.
                        # If dist_tp < dist_sl, likely TP.
                        
                        if dist_tp < dist_sl:
                            is_tp = True
                            event_price = tp_price
                            reason = "TP"
                            print(f"[DROP-INFER] Ticket={ticket} Leg=B -> TP (bid={current_price:.2f} closer to TP={tp_price:.2f} than SL={sl_price:.2f})")
                        else:
                            is_tp = False
                            event_price = sl_price
                            reason = "SL"
                            print(f"[DROP-INFER] Ticket={ticket} Leg=B -> SL (bid={current_price:.2f} closer to SL={sl_price:.2f} than TP={tp_price:.2f})")
                            
                    else:  # SELL position
                        current_price = ask
                        dist_tp = abs(current_price - tp_price)
                        dist_sl = abs(current_price - sl_price)
                        
                        if dist_tp < dist_sl:
                            is_tp = True
                            event_price = tp_price
                            reason = "TP"
                            print(f"[DROP-INFER] Ticket={ticket} Leg=S -> TP (ask={current_price:.2f} closer to TP={tp_price:.2f} than SL={sl_price:.2f})")
                        else:
                            is_tp = False
                            event_price = sl_price
                            reason = "SL"
                            print(f"[DROP-INFER] Ticket={ticket} Leg=S -> SL (ask={current_price:.2f} closer to SL={sl_price:.2f} than TP={tp_price:.2f})")

                # Determine completed/incomplete using IN-MEMORY pair state (not MT5 positions).
                # This remembers "ever filled" even if one leg already closed via SL.
                pair = self.pairs.get(pair_idx)
                was_completed = pair and pair.buy_filled and pair.sell_filled
                was_incomplete = not was_completed

                # RETIREMENT LOGIC: Permanently block re-entries after TP or SL hit
                if pair and not pair.tp_blocked:
                    if tp_touched or sl_touched or reason in ["TP", "SL"]:
                        pair.tp_blocked = True
                        print(f"[BLOCK] Pair {pair_idx} retired permanently (hit {reason})")

                        # Log TP/SL hit to group logger
                        if is_tp:
                            self.group_logger.log_tp_hit(
                                group_id=group_id,
                                pair_idx=pair_idx,
                                leg=leg,
                                price=event_price,
                                was_incomplete=was_incomplete
                            )
                        else:
                            self.group_logger.log_sl_hit(
                                group_id=group_id,
                                pair_idx=pair_idx,
                                leg=leg,
                                price=event_price
                            )

                # DEBUG: Trace pair flags to identify incorrect "completed" detection for INIT pairs
                if pair:
                    print(f"[DROP] Ticket={ticket} Pair={pair_idx} Leg={leg} Reason={reason} Price={event_price:.2f} "
                        f"buy_filled={pair.buy_filled} sell_filled={pair.sell_filled} "
                        f"Completed={was_completed} Group={group_id} Blocked={pair.tp_blocked}")
                else:
                    print(f"[DROP] Ticket={ticket} Pair={pair_idx} Leg={leg} Reason={reason} Price={event_price:.2f} "
                        f"Pair=None! Group={group_id}")

                # ROUTING
                # Determine direction from which leg hit TP (needed for expansion)
                is_bullish = (leg == 'B')  # Buy leg TP = price went up = bullish
                
                if is_tp:
                    if was_incomplete:
                        # INCOMPLETE PAIR TP -> Fire INIT for next group
                        # Triggered for ALL groups (including > 0)
                        
                        # Check duplicate prevention set first
                        if pair_idx in self._incomplete_pairs_init_triggered:
                            print(f"[TP-INCOMPLETE-BLOCKED] Pair={pair_idx} already fired INIT before, skipping")
                        elif self.graceful_stop:
                            print(f"[TP-INCOMPLETE] Pair={pair_idx} Group={group_id} -> graceful stop active, no INIT")
                        else:
                            print(f"[TP-INCOMPLETE] Pair={pair_idx} Group={group_id} -> Firing INIT for Group {self.current_group + 1} (Bullish={is_bullish})")
                            self._incomplete_pairs_init_triggered.add(pair_idx)
                            
                            # Pass triggering pair index so Init can fill the missing leg of previous group
                            await self._execute_group_init(self.current_group + 1, event_price, is_bullish_source=is_bullish, trigger_pair_idx=pair_idx)

                    else:
                        # Completed-pair TP
                        # FORCE NORMAL EXPANSION using High-Water C
                        # We do NOT skip based on live C dropping. We use high-water C to gate atomic/non-atomic logic inside.
                        
                        # Get verified high-water C
                        C_highwater = self._get_c_highwater(self.current_group)
                        
                        if pair_idx in self._pairs_tp_expanded:
                            print(f"[TP-BLOCKED] Pair={pair_idx} already fired expansion")
                            
                        elif group_id == self.current_group:
                            print(f"[TP-COMPLETE] Active Group {group_id} -> Executing Expansion (C_Highwater={C_highwater})")
                            # Call expansion regardless of C value (pass C_highwater so it knows if it should be atomic)
                            await self._execute_tp_expansion(group_id, event_price, is_bullish, C_highwater)
                            self._pairs_tp_expanded.add(pair_idx)
                            
                        elif group_id == self.current_group - 1:
                            print(f"[TP-COMPLETE] Prior Group {group_id} (Parent) -> Drive active group check")
                            await self._handle_completed_pair_expansion(event_price, is_bullish)
                            self._pairs_tp_expanded.add(pair_idx)
                        elif group_id < self.current_group - 1:
                            print(f"[TP-COMPLETE] Ancestor Group {group_id} < {self.current_group - 1} -> Ignoring for expansion (prevent double execution)")
                            # Still mark as expanded to prevent repeated logs, but don't drive expansion
                            self._pairs_tp_expanded.add(pair_idx)

                # Hedge close (leave your existing behavior)
                pair = self.pairs.get(pair_idx)
                if pair and pair.hedge_active and pair.hedge_ticket:
                    print(f"   [HEDGE] Closing hedge {pair.hedge_ticket}")
                    self._close_position(pair.hedge_ticket)

                # Cleanup (ticket is gone)
                self.ticket_map.pop(ticket, None)
                self.ticket_touch_flags.pop(ticket, None)
                await self.repository.delete_ticket(ticket)

            await self.save_state()

        except Exception as e:
            print(f"[ERROR] _check_position_drops: {e}")
            import traceback
            traceback.print_exc()

    def _check_if_tp_hit(self, ticket: int, direction: str) -> bool:
        """
        Check if a closed position hit TP (profit) or SL (loss).
        Returns True if TP was hit.
        """
        # Get deals for this position
        deals = mt5.history_deals_get(position=ticket)
        if not deals or len(deals) < 2:
            return False
        
        # The closing deal is the last one
        close_deal = deals[-1]
        
        # If profit > 0, TP was hit
        return close_deal.profit > 0
    
    async def _close_pair_positions(self, pair_index: int, direction_to_close: str):
        """
        Close all positions for a specific pair in a specific direction.
        FIXED: Aggressive Retry Logic. 
        If a close fails (e.g. slippage/requote), it retries 5 times 
        with increasing deviation to FORCE the position closed.
        """
        positions = mt5.positions_get(symbol=self.symbol)
        if not positions:
            return
        
        magic = 50000 + pair_index  # Our magic number for this pair
        
        for pos in positions:
            if pos.magic != magic:
                continue
            
            pos_direction = "buy" if pos.type == mt5.ORDER_TYPE_BUY else "sell"
            
            # Check if this position matches the direction we want to kill (or "both")
            if direction_to_close == "both" or pos_direction == direction_to_close:
                
                # --- AGGRESSIVE RETRY LOOP ---
                max_retries = 5
                for i in range(max_retries):
                    tick = mt5.symbol_info_tick(self.symbol)
                    if not tick:
                        await asyncio.sleep(0.1)
                        continue
                        
                    # Determine close type and price
                    close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                    close_price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
                    
                    # ESCALATING SLIPPAGE: Increase deviation by 20 on each fail
                    # Attempt 1: 20, Attempt 2: 40, ... Attempt 5: 100
                    current_deviation = 20 + (i * 20) 
                    
                    request = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": self.symbol,
                        "position": pos.ticket,
                        "volume": pos.volume,
                        "type": close_type,
                        "price": close_price,
                        "deviation": current_deviation, # Dynamic Slippage
                        "magic": magic,
                        "comment": f"Nuclear Close {pair_index} (Try {i+1})",
                    }
                    
                    result = mt5.order_send(request)
                    
                    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                        print(f" {self.symbol}: Closed {pos_direction.upper()} for Pair {pair_index} @ {close_price}")
                        break # Success - Exit the retry loop
                    
                    elif result:
                        print(f" {self.symbol}: Close failed ({result.comment}). Retrying {i+1}/{max_retries} with Dev={current_deviation}...")
                        await asyncio.sleep(0.2) # Short pause to let quotes refresh
                    
                    else:
                        print(f"[CLOSE] {self.symbol}: Order send failed. Retrying...")
                        await asyncio.sleep(0.2)
    
    def _close_position(self, ticket: int):
        """
        Close a single position by ticket number.
        Used by terminate() for nuclear reset.
        """
        position = mt5.positions_get(ticket=ticket)
        if not position:
            return False
        
        pos = position[0]
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return False
        
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        close_price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "position": ticket,
            "volume": pos.volume,
            "type": close_type,
            "price": close_price,
            "deviation": 50,
            "magic": pos.magic,
            "comment": "Terminate",
        }
        
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return True
        return False
                            
    def _count_triggered_pairs(self) -> int:
        """Count pairs that have executed at least one trade (trade_count > 0)."""
        return sum(1 for pair in self.pairs.values() if pair.trade_count > 0)
    
    def _find_furthest_untriggered(self, direction: str) -> int:
        """
        Find the FURTHEST untriggered pair (trade_count == 0).
        
        Args:
            direction: "up" for leapfrog up (find lowest untriggered), 
                      "down" for leapfrog down (find highest untriggered)
        
        Returns:
            Index of the furthest untriggered pair, or None if all triggered
        """
        untriggered = [idx for idx, pair in self.pairs.items() if pair.trade_count == 0]
        if not untriggered:
            return None
        
        if direction == "up":
            # Leapfrog UP: find the LOWEST (most negative) untriggered pair
            return min(untriggered)
        else:
            # Leapfrog DOWN: find the HIGHEST (most positive) untriggered pair
            return max(untriggered)
    
    async def _reopen_pair_at_same_level(self, pair_index: int):
        """Re-arm triggers at the same price level after TP/SL hit (no leapfrog)."""
        pair = self.pairs.get(pair_index)
        if not pair:
            return
        
        # Note: trade_count should already be reset by caller
        # If not, reset it here as safety
        if pair.trade_count != 0:
            pair.trade_count = 0
        
        # Get current tick to check if we should execute immediately
        tick = mt5.symbol_info_tick(self.symbol)
        
        # Re-arm buy trigger
        pair.buy_pending_ticket = self._place_pending_order(
            self._get_order_type("buy", pair.buy_price),
            pair.buy_price, pair_index
        )
        # Re-arm sell trigger
        pair.sell_pending_ticket = self._place_pending_order(
            self._get_order_type("sell", pair.sell_price),
            pair.sell_price, pair_index
        )
        
        # [FIX 3] Check if price is already at trigger level and execute immediately
        # This prevents the scenario where price is in zone but trigger won't fire
        # because the zone logic requires price to LEAVE and RETURN
        if tick and pair.next_action:
            if pair.next_action == "buy":
                # Check if price at buy level using grid polarity logic
                if pair_index > 0:
                    buy_triggered = tick.ask >= pair.buy_price
                elif pair_index < 0:
                    buy_triggered = tick.bid <= pair.buy_price
                else:  # pair_index == 0
                    buy_triggered = tick.ask >= pair.buy_price
                
                if buy_triggered and not pair.buy_filled:
                    print(f"[REOPEN] {self.symbol}: Price at BUY level - using locked execution")
                    # Use locked execution to prevent race conditions
                    if await self._execute_trade_with_chain("buy", pair_index):
                        return  # Exit after successful execution
            
            elif pair.next_action == "sell":
                # Check if price at sell level using grid polarity logic
                if pair_index > 0:
                    sell_triggered = tick.ask >= pair.sell_price
                elif pair_index < 0:
                    sell_triggered = tick.bid <= pair.sell_price
                else:  # pair_index == 0
                    sell_triggered = tick.bid <= pair.sell_price
                
                if sell_triggered and not pair.sell_filled:
                    print(f"[REOPEN] {self.symbol}: Price at SELL level - using locked execution")
                    # Use locked execution to prevent race conditions
                    if await self._execute_trade_with_chain("sell", pair_index):
                        return  # Exit after successful execution
        
        print(f"[REOPEN] {self.symbol}: Pair {pair_index} re-armed at same levels (B@{pair.buy_price:.2f}, S@{pair.sell_price:.2f}) - lot reset to first")
    
    async def _create_next_positive_pair(self, edge_idx: int):
        """
        Create the next positive pair beyond the current edge.
        Called when edge positive pair triggers - expands grid upward.
        
        New pair structure: S[n+1] = B[n], B[n+1] = S[n+1] + spread
        """
        edge_pair = self.pairs.get(edge_idx)
        if not edge_pair:
            return
        
        # [DIRECTIONAL GUARD] Bullish Expansion Restriction
        # Use per-group tracking for direction guards
        init_source = self.group_init_source.get(self.current_group)
        
        # RULE: If Init was BULLISH, we BLOCK Bullish Natural Expansion
        # We only allow Bearish Natural Expansion (Retracement)
        if init_source == "BULLISH":
            # print(f"[GUARD] Blocking Bullish expansion (Init was BULLISH, expecting BEARISH retracement)")
            return

        tick = mt5.symbol_info_tick(self.symbol)
        new_idx = edge_idx + 1
        
        # [FIX #1] Guard: If this pair already exists and SELL is filled (from chain), skip
        existing_pair = self.pairs.get(new_idx)
        if existing_pair and existing_pair.sell_filled:
            print(f" {self.symbol}: Pair {new_idx} already has SELL filled (from chain). Skipping expansion.")
            return
        
        # New pair: S at edge's B price, B at S + spread
        new_sell_price = edge_pair.buy_price  # Chain: S[n+1] = B[n]
        new_buy_price = new_sell_price + self.spread
        
        new_pair = GridPair(
            index=new_idx,
            buy_price=new_buy_price,
            sell_price=new_sell_price
        )
        
        # Positive pairs START with SELL
        new_pair.next_action = "sell"
        self.pairs[new_idx] = new_pair
        
        # --- EXECUTE SELL IMMEDIATELY ---
        print(f" {self.symbol}: Creating Pair {new_idx} (ABOVE). Executing S@{new_sell_price:.2f} immediately.")
        
        # Use calculated price, execute at market
        ticket = await self._execute_market_order("sell", new_sell_price, new_idx)
        
        if ticket:
            new_pair.sell_filled = True
            new_pair.sell_ticket = ticket
            new_pair.sell_in_zone = True
            
            # FIX: Increment trade count (0 -> 1) and toggle to 'buy'
            # This ensures the NEXT trade (Buy) uses the 2nd lot size (0.02)
            new_pair.advance_toggle() 
            
            # Arm the BUY trigger (Buy Stop)
            new_pair.buy_pending_ticket = self._place_pending_order("buy_stop", new_buy_price, new_idx)
            
            # [FIX #3] Chain: If edge pair's BUY is at same price as new SELL, execute it
            if not edge_pair.buy_filled and edge_pair.trade_count < self.max_positions:
                if abs(edge_pair.buy_price - new_sell_price) < 1.0:
                    print(f" {self.symbol}: CHAIN B{edge_idx} @ {edge_pair.buy_price:.2f} (from expansion)")
                    chain_ticket = await self._execute_market_order("buy", edge_pair.buy_price, edge_idx)
                    if chain_ticket:
                        edge_pair.buy_filled = True
                        edge_pair.buy_ticket = chain_ticket
                        edge_pair.buy_pending_ticket = 0
                        edge_pair.buy_in_zone = True
                        edge_pair.advance_toggle()
            
            print(f" {self.symbol}: Pair {new_idx} Active. S filled (0.01), B pending (0.02) @ {new_buy_price:.2f}")
        else:
            # Fallback
            new_pair.sell_pending_ticket = self._place_pending_order("sell_limit", new_sell_price, new_idx)
            new_pair.buy_pending_ticket = self._place_pending_order("buy_stop", new_buy_price, new_idx)
        
        await self.save_state()

    async def _create_next_negative_pair(self, edge_idx: int):
        """
        Create the next negative pair beyond the current edge.
        Called when edge negative pair triggers - expands grid downward.
        
        New pair structure: B[n-1] = S[n], S[n-1] = B[n-1] - spread
        """
        edge_pair = self.pairs.get(edge_idx)
        if not edge_pair:
            return
        
        # [DIRECTIONAL GUARD] Bearish Expansion Restriction
        # Use per-group tracking for direction guards
        init_source = self.group_init_source.get(self.current_group)
        
        # RULE: If Init was BEARISH, we BLOCK Bearish Natural Expansion
        # We only allow Bullish Natural Expansion (Retracement)
        if init_source == "BEARISH":
            # print(f"[GUARD] Blocking Bearish expansion (Init was BEARISH, expecting BULLISH retracement)")
            return
        
        new_idx = edge_idx - 1
        
        # [FIX #1] Guard: If this pair already exists and BUY is filled (from chain), skip
        existing_pair = self.pairs.get(new_idx)
        if existing_pair and existing_pair.buy_filled:
            print(f" {self.symbol}: Pair {new_idx} already has BUY filled (from chain). Skipping expansion.")
            return
        
        # New pair: B at edge's S price, S at B - spread
        new_buy_price = edge_pair.sell_price  # Chain: B[n-1] = S[n]
        new_sell_price = new_buy_price - self.spread
        
        new_pair = GridPair(
            index=new_idx,
            buy_price=new_buy_price,
            sell_price=new_sell_price
        )
        
        # Negative pairs START with BUY
        new_pair.next_action = "buy"
        self.pairs[new_idx] = new_pair
        
        # --- EXECUTE BUY IMMEDIATELY ---
        print(f" {self.symbol}: Creating Pair {new_idx} (BELOW). Executing B@{new_buy_price:.2f} immediately.")
        
        # Use calculated price, execute at market
        ticket = await self._execute_market_order("buy", new_buy_price, new_idx)
        
        if ticket:
            new_pair.buy_filled = True
            new_pair.buy_ticket = ticket
            new_pair.buy_in_zone = True
            
            # FIX: Increment trade count (0 -> 1) and toggle to 'sell'
            # This ensures the NEXT trade (Sell) uses the 2nd lot size (0.02)
            new_pair.advance_toggle()
            
            # Arm the SELL trigger (Sell Stop)
            new_pair.sell_pending_ticket = self._place_pending_order("sell_stop", new_sell_price, new_idx)
            
            # [FIX #3] Chain: If edge pair's SELL is at same price as new BUY, execute it
            if not edge_pair.sell_filled and edge_pair.trade_count < self.max_positions:
                if abs(edge_pair.sell_price - new_buy_price) < 1.0:
                    print(f" {self.symbol}: CHAIN S{edge_idx} @ {edge_pair.sell_price:.2f} (from expansion)")
                    chain_ticket = await self._execute_market_order("sell", edge_pair.sell_price, edge_idx)
                    if chain_ticket:
                        edge_pair.sell_filled = True
                        edge_pair.sell_ticket = chain_ticket
                        edge_pair.sell_pending_ticket = 0
                        edge_pair.advance_toggle()
            
            print(f" {self.symbol}: Pair {new_idx} Active. B filled (0.01), S pending (0.02) @ {new_sell_price:.2f}")
        else:
            # Fallback
            new_pair.buy_pending_ticket = self._place_pending_order("buy_limit", new_buy_price, new_idx)
            new_pair.sell_pending_ticket = self._place_pending_order("sell_stop", new_sell_price, new_idx)
        
        await self.save_state()
    async def _do_leapfrog_untriggered_up(self, untriggered_idx: int):
        """
        Leapfrog a specific UNTRIGGERED pair to the top (price trending UP).
        
        - Remove the untriggered pair
        - Create new pair at top with SELL immediately at market
        - Set BUY trigger at spread above
        """
        # Acquire global lock to prevent concurrent leapfrog operations
        async with self.execution_lock:
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                print(f" {self.symbol}: LEAPFROG UNTRIGGERED UP failed - no tick data")
                return
            
            indices = sorted(self.pairs.keys())
            max_idx = indices[-1]
            new_idx = max_idx + 1
            
            # Get and remove the untriggered pair
            untriggered_pair = self.pairs.get(untriggered_idx)
            if untriggered_pair:
                self._cancel_pair_orders(untriggered_pair)
                del self.pairs[untriggered_idx]
            
            # Create new pair at top - SELL immediately at market
            exec_sell_price = tick.bid
            new_buy_price = exec_sell_price + self.spread
            
            new_pair = GridPair(
                index=new_idx,
                buy_price=new_buy_price,
                sell_price=exec_sell_price
            )
            self.pairs[new_idx] = new_pair
            
            # Execute SELL immediately
            ticket = await self._execute_market_order("sell", exec_sell_price, new_idx)
            
            if ticket:
                new_pair.sell_filled = True
                new_pair.sell_ticket = ticket
                new_pair.sell_in_zone = True
                new_pair.advance_toggle()  # Now next_action = "buy"
                
                # [CHAIN FIX] Backward chain: S[new] triggers → B[new-1] must also trigger
                prev_idx = new_idx - 1
                prev_pair = self.pairs.get(prev_idx)
                if prev_pair and not prev_pair.buy_filled and prev_pair.trade_count < self.max_positions:
                    price_diff = abs(prev_pair.buy_price - exec_sell_price)
                    if price_diff < 11.0:
                        print(f"[LEAPFROG CHAIN] {self.symbol}: B{prev_idx} @ {prev_pair.buy_price:.2f} (chained from S{new_idx})")
                        chain_ticket = await self._execute_market_order("buy", prev_pair.buy_price, prev_idx)
                        if chain_ticket:
                            prev_pair.buy_filled = True
                            prev_pair.buy_ticket = chain_ticket
                            prev_pair.buy_pending_ticket = 0
                            prev_pair.buy_in_zone = True
                            prev_pair.advance_toggle()
                
                # Arm BUY trigger
                new_pair.buy_pending_ticket = self._place_pending_order("buy_stop", new_buy_price, new_idx)
                
                print(f" {self.symbol}: LEAPFROG UNTRIGGERED UP | Pair {untriggered_idx} -> {new_idx} | SELL@{exec_sell_price:.2f}")
            else:
                # Failed - just arm triggers
                new_pair.sell_pending_ticket = self._place_pending_order("sell_limit", exec_sell_price, new_idx)
                new_pair.buy_pending_ticket = self._place_pending_order("buy_stop", new_buy_price, new_idx)
            
            self.print_grid_table()
            await self.save_state()
    
    async def _do_leapfrog_untriggered_down(self, untriggered_idx: int):
        """
        Leapfrog a specific UNTRIGGERED pair to the bottom (price trending DOWN).
        
        - Remove the untriggered pair
        - Create new pair at bottom with BUY immediately at market
        - Set SELL trigger at spread below
        """
        # Acquire global lock to prevent concurrent leapfrog operations
        async with self.execution_lock:
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                print(f" {self.symbol}: LEAPFROG UNTRIGGERED DOWN failed - no tick data")
                return
            
            indices = sorted(self.pairs.keys())
            min_idx = indices[0]
            new_idx = min_idx - 1
            
            # Get and remove the untriggered pair
            untriggered_pair = self.pairs.get(untriggered_idx)
            if untriggered_pair:
                self._cancel_pair_orders(untriggered_pair)
                del self.pairs[untriggered_idx]
            
            # Create new pair at bottom - BUY immediately at market
            exec_buy_price = tick.ask
            new_sell_price = exec_buy_price - self.spread
            
            new_pair = GridPair(
                index=new_idx,
                buy_price=exec_buy_price,
                sell_price=new_sell_price
            )
            self.pairs[new_idx] = new_pair
            
            # Execute BUY immediately
            ticket = await self._execute_market_order("buy", exec_buy_price, new_idx)
            
            if ticket:
                new_pair.buy_filled = True
                new_pair.buy_ticket = ticket
                new_pair.buy_in_zone = True
                new_pair.advance_toggle()  # Now next_action = "sell"
                
                # [CHAIN FIX] Forward chain: B[new] triggers → S[new+1] must also trigger
                next_idx = new_idx + 1
                next_pair = self.pairs.get(next_idx)
                if next_pair and not next_pair.sell_filled and next_pair.trade_count < self.max_positions:
                    price_diff = abs(next_pair.sell_price - exec_buy_price)
                    if price_diff < 11.0:
                        print(f"[LEAPFROG CHAIN] {self.symbol}: S{next_idx} @ {next_pair.sell_price:.2f} (chained from B{new_idx})")
                        chain_ticket = await self._execute_market_order("sell", next_pair.sell_price, next_idx)
                        if chain_ticket:
                            next_pair.sell_filled = True
                            next_pair.sell_ticket = chain_ticket
                            next_pair.sell_pending_ticket = 0
                            next_pair.sell_in_zone = True
                            next_pair.advance_toggle()
                
                # Arm SELL trigger
                new_pair.sell_pending_ticket = self._place_pending_order("sell_stop", new_sell_price, new_idx)
                
                print(f" {self.symbol}: LEAPFROG UNTRIGGERED DOWN | Pair {untriggered_idx} -> {new_idx} | BUY@{exec_buy_price:.2f}")
            else:
                # Failed - just arm triggers
                new_pair.buy_pending_ticket = self._place_pending_order("buy_limit", exec_buy_price, new_idx)
                new_pair.sell_pending_ticket = self._place_pending_order("sell_stop", new_sell_price, new_idx)
            
            self.print_grid_table()
            await self.save_state()
    
    async def _do_leapfrog_up(self):
        """
        Leapfrog the bottom pair to the top (price trending UP).
        
        SMART EXECUTION:
        - Price is trending UP, so SELL immediately at current market (bid)
        - Set BUY trigger at SELL price + spread (above the sell)
        - This captures the upward movement with the sell, then prepares for reversal
        
        Toggle: After sell executes, next_action = "buy"
        """
        # Acquire global lock to prevent concurrent leapfrog operations
        async with self.execution_lock:
            if len(self.pairs) < 2:
                return
            
            # Get current market price
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                print(f" {self.symbol}: LEAPFROG UP failed - no tick data")
                return
                
            indices = sorted(self.pairs.keys())
            min_idx = indices[0]
            max_idx = indices[-1]
            
            new_idx = max_idx + 1
            
            bottom_pair = self.pairs[min_idx]
            
            # Cancel bottom pair's pending orders and remove it
            self._cancel_pair_orders(bottom_pair)
            del self.pairs[min_idx]
            
            # SMART LEAPFROG UP:
            # - Execute SELL immediately at current bid (market price for sells)
            # - Set BUY trigger at sell_price + spread (above)
            exec_sell_price = tick.bid
            new_buy_price = exec_sell_price + self.spread
            
            new_pair = GridPair(
                index=new_idx,
                buy_price=new_buy_price,
                sell_price=exec_sell_price
            )
            self.pairs[new_idx] = new_pair
            
            # Execute SELL immediately at market
            ticket = await self._execute_market_order("sell", exec_sell_price, new_idx)
            
            if ticket:
                new_pair.sell_filled = True
                new_pair.sell_ticket = ticket
                new_pair.sell_in_zone = True
                new_pair.advance_toggle()  # Now next_action = "buy"
                
                # [CHAIN FIX] Backward chain: S[new] triggers → B[new-1] must also trigger  
                prev_idx = new_idx - 1
                prev_pair = self.pairs.get(prev_idx)
                if prev_pair and not prev_pair.buy_filled and prev_pair.trade_count < self.max_positions:
                    price_diff = abs(prev_pair.buy_price - exec_sell_price)
                    if price_diff < 11.0:
                        print(f"[LEAPFROG CHAIN] {self.symbol}: B{prev_idx} @ {prev_pair.buy_price:.2f} (chained from S{new_idx})")
                        chain_ticket = await self._execute_market_order("buy", prev_pair.buy_price, prev_idx)
                        if chain_ticket:
                            prev_pair.buy_filled = True
                            prev_pair.buy_ticket = chain_ticket
                            prev_pair.buy_pending_ticket = 0
                            prev_pair.buy_in_zone = True
                            prev_pair.advance_toggle()
                
                # Arm BUY trigger at spread above
                new_pair.buy_pending_ticket = self._place_pending_order("buy_stop", new_buy_price, new_idx)
                
                # Log leapfrog event
                await self._log_trade(
                    event_type="LEAPFROG_UP",
                    pair_index=new_idx,
                    direction="SELL",
                    price=exec_sell_price,
                    lot_size=new_pair.get_next_lot(self.lot_sizes) or 0.0,
                    ticket=ticket,
                    notes=f"Pair {min_idx} -> {new_idx} | SELL@MKT, B@{new_buy_price:.2f}",
                    trade_count=new_pair.trade_count
                )
            else:
                # Failed to execute, just set up triggers
                new_pair.next_action = "sell"
                new_pair.sell_pending_ticket = self._place_pending_order("sell_limit", exec_sell_price, new_idx)
                new_pair.buy_pending_ticket = self._place_pending_order("buy_stop", new_buy_price, new_idx)
                print(f" {self.symbol}: LEAPFROG UP sell failed, armed triggers instead")
            
            # Print grid table after leapfrog for visualization
            self.print_grid_table()
            await self.save_state()
    
    async def _do_leapfrog_down(self):
        """
        Leapfrog the top pair to the bottom (price trending DOWN).
        
        SMART EXECUTION:
        - Price is trending DOWN, so BUY immediately at current market (ask)
        - Set SELL trigger at BUY price - spread (below the buy)
        - This catches the bottom with the buy, then prepares for reversal
        
        Toggle: After buy executes, next_action = "sell"
        """
        # Acquire global lock to prevent concurrent leapfrog operations
        async with self.execution_lock:
            if len(self.pairs) < 2:
                return
            
            # Get current market price
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                print(f" {self.symbol}: LEAPFROG DOWN failed - no tick data")
                return
                
            indices = sorted(self.pairs.keys())
            min_idx = indices[0]
            max_idx = indices[-1]
            
            new_idx = min_idx - 1
            
            top_pair = self.pairs[max_idx]
            
            # Cancel top pair's pending orders and remove it
            self._cancel_pair_orders(top_pair)
            del self.pairs[max_idx]
            
            # SMART LEAPFROG DOWN:
            # - Execute BUY immediately at current ask (market price for buys)
            # - Set SELL trigger at buy_price - spread (below)
            exec_buy_price = tick.ask
            new_sell_price = exec_buy_price - self.spread
            
            new_pair = GridPair(
                index=new_idx,
                buy_price=exec_buy_price,
                sell_price=new_sell_price
            )
            self.pairs[new_idx] = new_pair
            
            # Execute BUY immediately at market
            ticket = await self._execute_market_order("buy", exec_buy_price, new_idx)
            
            if ticket:
                new_pair.buy_filled = True
                new_pair.buy_ticket = ticket
                new_pair.buy_in_zone = True
                new_pair.advance_toggle()  # Now next_action = "sell"
                
                # [CHAIN FIX] Forward chain: B[new] triggers → S[new+1] must also trigger
                next_idx = new_idx + 1
                next_pair = self.pairs.get(next_idx)
                if next_pair and not next_pair.sell_filled and next_pair.trade_count < self.max_positions:
                    price_diff = abs(next_pair.sell_price - exec_buy_price)
                    if price_diff < 11.0:
                        print(f"[LEAPFROG CHAIN] {self.symbol}: S{next_idx} @ {next_pair.sell_price:.2f} (chained from B{new_idx})")
                        chain_ticket = await self._execute_market_order("sell", next_pair.sell_price, next_idx)
                        if chain_ticket:
                            next_pair.sell_filled = True
                            next_pair.sell_ticket = chain_ticket
                            next_pair.sell_pending_ticket = 0
                            next_pair.sell_in_zone = True
                            next_pair.advance_toggle()
                
                # Arm SELL trigger at spread below
                new_pair.sell_pending_ticket = self._place_pending_order("sell_stop", new_sell_price, new_idx)
                
                # Log leapfrog event
                await self._log_trade(
                    event_type="LEAPFROG_DOWN",
                    pair_index=new_idx,
                    direction="BUY",
                    price=exec_buy_price,
                    lot_size=new_pair.get_next_lot(self.lot_sizes) or 0.0,
                    ticket=ticket,
                    notes=f"Pair {max_idx} -> {new_idx} | BUY@MKT, S@{new_sell_price:.2f}"
                )
            else:
                # Failed to execute, just set up triggers
                new_pair.next_action = "buy"
                new_pair.buy_pending_ticket = self._place_pending_order("buy_limit", exec_buy_price, new_idx)
                new_pair.sell_pending_ticket = self._place_pending_order("sell_stop", new_sell_price, new_idx)
                print(f" {self.symbol}: LEAPFROG DOWN buy failed, armed triggers instead")
            
            # Print grid table after leapfrog for visualization
            self.print_grid_table()
            await self.save_state()
    
    async def _check_leapfrog(self, ask: float, bid: float):
        """
        Check if price has moved beyond the grid and execute Leapfrog.
        
        IMPORTANT: max_pairs defines the NUMBER of grid levels:
        - max_pairs=3 → indices: -1, 0, +1 (max_level = 1)
        - max_pairs=5 → indices: -2, -1, 0, +1, +2 (max_level = 2)
        - max_pairs=7 → indices: -3 to +3 (max_level = 3)
        etc.
        
        Leapfrog should NOT create pairs beyond max_level.
        """
        if len(self.pairs) < self.max_pairs:
            return  # Still expanding, don't leapfrog yet
        
        # Calculate max level (how far from center we can go)
        max_level = (self.max_pairs - 1) // 2  # e.g., max_pairs=3 → max_level=1
        
        indices = sorted(self.pairs.keys())
        min_idx = indices[0]
        max_idx = indices[-1]
        
        top_pair = self.pairs[max_idx]
        bottom_pair = self.pairs[min_idx]
        
        # Bullish Leapfrog: Price above top pair's buy price + spread
        if ask > top_pair.buy_price + self.spread:
            # Check if we can go higher (within max_level)
            new_idx = max_idx + 1
            if new_idx > max_level:
                # At max level, can't leapfrog up further
                return
            
            # Take lowest pair and move to top
            self._cancel_pair_orders(bottom_pair)
            del self.pairs[min_idx]
            
            # Create new pair above top - follow ladder structure
            new_sell_price = top_pair.buy_price  # New SELL = Top's BUY
            new_buy_price = new_sell_price + self.spread  # BUY is spread above
            
            new_pair = GridPair(
                index=new_idx,
                buy_price=new_buy_price,
                sell_price=new_sell_price
            )
            new_pair.sell_pending_ticket = self._place_pending_order("sell_limit", new_sell_price, new_idx)
            new_pair.buy_pending_ticket = self._place_pending_order("buy_stop", new_buy_price, new_idx)
            self.pairs[new_idx] = new_pair
            
            print(f" {self.symbol}: LEAPFROG UP! Pair {min_idx} -> Pair {new_idx}. B@{new_buy_price:.2f} S@{new_sell_price:.2f}")
            await self.save_state()
        
        # Bearish Leapfrog: Price below bottom pair's sell price - spread
        elif bid < bottom_pair.sell_price - self.spread:
            # Check if we can go lower (within -max_level)
            new_idx = min_idx - 1
            if new_idx < -max_level:
                # At min level, can't leapfrog down further
                return
            
            # Take highest pair and move to bottom
            self._cancel_pair_orders(top_pair)
            del self.pairs[max_idx]
            
            # Create new pair below bottom - follow ladder structure
            new_buy_price = bottom_pair.sell_price  # New BUY = Bottom's SELL
            new_sell_price = new_buy_price - self.spread  # SELL is spread below
            
            new_pair = GridPair(
                index=new_idx,
                buy_price=new_buy_price,
                sell_price=new_sell_price
            )
            new_pair.buy_pending_ticket = self._place_pending_order("buy_limit", new_buy_price, new_idx)
            new_pair.sell_pending_ticket = self._place_pending_order("sell_stop", new_sell_price, new_idx)
            self.pairs[new_idx] = new_pair
            
            print(f" {self.symbol}: LEAPFROG DOWN! Pair {max_idx} -> Pair {new_idx}. B@{new_buy_price:.2f} S@{new_sell_price:.2f}")
            await self.save_state()
    
    # ========================================================================
    # ORDER EXECUTION HELPERS
    # ========================================================================
    
    def _get_order_type(self, direction: str, price: float) -> str:
        """Determine order type based on direction and price relative to current."""
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return "buy_stop" if direction == "buy" else "sell_stop"
        
        if direction == "buy":
            if price > tick.ask:
                return "buy_stop"
            else:
                return "buy_limit"
        else:
            if price < tick.bid:
                return "sell_stop"
            else:
                return "sell_limit"
    
    def _get_reopen_order_type(self, direction: str, pair_idx: int) -> str:
        """
        Determine order type for RE-OPENED positions after TP/SL hits.
        Uses the SAME order types as initial grid creation based on grid polarity.
        
        POSITIVE GRID (idx > 0):
        - BUY = BUY_STOP (buy above market)
        - SELL = SELL_LIMIT (sell above market)
        
        NEGATIVE GRID (idx <= 0):
        - BUY = BUY_LIMIT (buy below market)
        - SELL = SELL_STOP (sell below market)
        """
        if pair_idx > 0:  # Positive grid
            if direction == "buy":
                return "buy_stop"
            else:
                return "sell_limit"
        else:  # Negative grid (including pair 0)
            if direction == "buy":
                return "buy_limit"
            else:
                return "sell_stop"
    
    def _get_filling_mode(self):
        """Get the correct filling mode for this symbol."""
        
        symbol_info = mt5.symbol_info(self.symbol)
        if not symbol_info:
            return mt5.ORDER_FILLING_FOK  # Default for Deriv
        
        # Check which modes are supported (filling_mode is a bitmask)
        # Bitmask values: FOK=1, IOC=2, RETURN=4 (or similar depending on broker)
        filling = symbol_info.filling_mode
        
        # For Deriv synthetics, FOK (value 0) typically works
        # Try FOK first
        if filling & 1:  # FOK supported
            return mt5.ORDER_FILLING_FOK
        elif filling & 2:  # IOC supported
            return mt5.ORDER_FILLING_IOC
        else:
            # Just use FOK as default for Deriv synthetics
            return mt5.ORDER_FILLING_FOK
    
    def _get_lot_size(self, index: int, direction: str = None) -> float:
        """
        Get lot size for a trade based on the pair's trade_count.
        
        Lot sizing is SEQUENTIAL per pair (NOT per direction):
        - 1st trade uses lot_sizes[0]
        - 2nd trade uses lot_sizes[1]
        - etc.
        
        Returns None if pair has reached max_positions (trade blocked).
        """
        pair = self.pairs.get(index)
        if not pair:
            # Pair not found, use first lot
            return self.lot_sizes[0] if self.lot_sizes else 0.01
        
        # Use trade_count based lot sizing (returns None if at max)
        return pair.get_next_lot(self.lot_sizes)

    
    def _place_pending_order(self, order_type: str, price: float, index: int) -> int:
        """
        VIRTUAL PENDING ORDER: Store trigger price but don't place actual MT5 pending order.
        Returns a fake ticket (negative index) as placeholder. Actual orders fire on trigger hit.
        """
        # Just log the virtual order - actual execution happens in tick monitoring
        print(f" {self.symbol}: Virtual {order_type.upper()} @ {price:.2f} (L{index})")
        
        # Return a fake ticket (we use negative numbers to indicate virtual orders)
        # The actual ticket will be assigned when the market order fires
        return -(index * 1000 + (1 if "buy" in order_type else 2))
    
    def _position_exists_for_trade(self, pair_idx: int, direction: str) -> bool:
        """
        Check MT5 directly to see if a position already exists for this trade.
        
        Uses trade_count INDEX (not value) to determine expected lot size.
        This is more reliable than flags because:
        - No stale flags after TP/SL
        - No race conditions
        - Natural re-trigger support
        
        Returns True if position exists with matching lot size for current trade_count.
        """
        pair = self.pairs.get(pair_idx)
        if not pair:
            return False
        
        # Get expected lot size for this trade (based on trade_count index)
        expected_lot = pair.get_next_lot(self.lot_sizes)
        if expected_lot is None:
            return True  # At max positions, block trade
        
        # Get all positions for this symbol
        positions = mt5.positions_get(symbol=self.symbol)
        if not positions:
            return False  # No positions exist
        
        # Check if any position matches our criteria
        target_price = pair.buy_price if direction == "buy" else pair.sell_price
        order_type = 0 if direction == "buy" else 1  # 0=BUY, 1=SELL in MT5
        
        for pos in positions:
            price_match = abs(pos.price_open - target_price) < 5.0  # Within 5.0 tolerance
            lot_match = abs(pos.volume - expected_lot) < 0.001
            type_match = pos.type == order_type
            
            if price_match and lot_match and type_match:
                return True  # Position already exists
        
        return False  # No matching position found
    
    async def _execute_trade_with_chain(self, direction: str, pair_idx: int) -> bool:
        """
        ATOMIC TRADE EXECUTION: Execute B[n] + S[n+1] or S[n] + B[n-1] as a single locked operation.
        """
        pair = self.pairs.get(pair_idx)
        if not pair:
            return False
        
        if self.trade_in_progress.get(pair_idx, False):
            return False
        
        async with self.execution_lock:
            if self.trade_in_progress.get(pair_idx, False):
                return False
            
            self.trade_in_progress[pair_idx] = True
            
            try:
                # CRITICAL: Validate toggle
                if pair.next_action != direction:
                    print(f" {self.symbol}: TOGGLE MISMATCH - Expected {pair.next_action}, got {direction}. Skipping.")
                    return False
                
                # CHECK: Max positions hard cap
                if pair.trade_count >= self.max_positions:
                    return False
                
                # ============================================
                # POSITION-BASED RESET LOGIC (YOUR REQUEST)
                # ============================================
                # Check if ANY position exists for this pair in MT5
                positions = mt5.positions_get(symbol=self.symbol)
                pair_positions = []
                if positions:
                    pair_positions = [p for p in positions if p.magic == 50000 + pair_idx]
                
                # If NO positions exist for this pair, log it (but do NOT reset trade_count here)
                # NOTE: trade_count reset is ONLY handled by _check_tp_sl_from_history
                if not pair_positions:
                    print(f" {self.symbol}: Pair {pair_idx} has NO active positions (trade_count={pair.trade_count})")
                else:
                    print(f" {self.symbol}: Pair {pair_idx} has {len(pair_positions)} active positions, trade_count={pair.trade_count}")
                # ============================================
                
                # Execute the trade
                price = pair.buy_price if direction == "buy" else pair.sell_price
                print(f" {self.symbol}: {direction.upper()} @ Pair {pair_idx} ({price:.2f}) [LOCKED]")
                ticket = await self._execute_market_order(direction, price, pair_idx)
                
                if not ticket:
                    print(f" {self.symbol}: {direction.upper()} failed for Pair {pair_idx}")
                    return False
                

                # Update pair state
                if direction == "buy":
                    pair.buy_filled = True
                    pair.buy_ticket = ticket
                    pair.buy_pending_ticket = 0
                else:
                    pair.sell_filled = True
                    pair.sell_ticket = ticket
                    pair.sell_pending_ticket = 0
                
                pair.is_reopened = False
                if direction == "buy":
                    pair.buy_in_zone = True
                else:
                    pair.sell_in_zone = True
                
                pair.record_position_open(ticket)
                pair.advance_toggle()
                
                # ============================================
                # ATOMIC HEDGE (Moved from Polling Loop)
                # ============================================
                if pair.trade_count >= self.max_positions and self.hedge_enabled:
                    # Deterministic Hedge Direction Logic
                    hedge_dir = None
                    is_odd = (self.max_positions % 2 != 0)
                    
                    if pair_idx <= 0: # Zero & Negative Pairs
                        if is_odd: hedge_dir = "sell"
                        else:      hedge_dir = "buy"
                    else: # Positive Pairs
                        if is_odd: hedge_dir = "buy"
                        else:      hedge_dir = "sell"
                    
                    print(f" {self.symbol}: [HEDGE TRIGGER] Pair {pair_idx} hit Max {self.max_positions}. executing {hedge_dir.upper()} hedge.")
                    # Execute immediately inside the lock
                    await self._execute_hedge(pair_idx, hedge_dir)

                # --- CHAIN EXECUTION (GAP FILLING GUARD) ---
                if direction == "buy":
                    # Forward chain: B[n] -> S[n+1]
                    next_idx = pair_idx + 1
                    if next_idx in self.pairs:
                        next_pair = self.pairs[next_idx]
                        if not next_pair.sell_filled: # GAP FILLING GUARD
                             print(f" {self.symbol}: Chaining B{pair_idx} -> S{next_idx}")
                             await self._execute_trade_with_chain("sell", next_idx)
                        else:
                             print(f" {self.symbol}: Skipped Chain S{next_idx} (Already Filled)")
                    elif next_idx <= (self.max_pairs - 1) // 2: # Check bounds
                        print(f" {self.symbol}: Creating Next Pair {next_idx} from Chain")
                        # (Logic to create next pair omitted, handled by expansion loop?)
                        self._create_next_positive_pair(pair_idx)

                elif direction == "sell":
                    # Backward chain: S[n] -> B[n-1]
                    next_idx = pair_idx - 1
                    if next_idx in self.pairs:
                         next_pair = self.pairs[next_idx]
                         if not next_pair.buy_filled: # GAP FILLING GUARD
                             print(f" {self.symbol}: Chaining S{pair_idx} -> B{next_idx}")
                             await self._execute_trade_with_chain("buy", next_idx)
                         else:
                             print(f" {self.symbol}: Skipped Chain B{next_idx} (Already Filled)")
                    elif abs(next_idx) <= (self.max_pairs - 1) // 2:
                        print(f" {self.symbol}: Creating Next Negative Pair {next_idx} from Chain")
                        await self._create_next_negative_pair(pair_idx)
                
                await self.save_state()
                return True
                
            finally:
                self.trade_in_progress[pair_idx] = False
    
    
    async def _check_virtual_triggers(self, ask: float, bid: float):
        """
        Check triggers and fire market orders.
        FIXED: 
        1. Removes 'Latch' logic that prevented re-entry after first fill.
        2. Uses trade_count < max_positions as the primary guard.
        3. PROXIMITY-BASED RE-ENTRY: Reopened pairs wait for price to TOUCH the level.
        """
        sorted_items = sorted(self.pairs.items(), key=lambda x: x[0])
        
        # Tolerance for proximity check (price must be within this distance to "touch" the level)
        tolerance = self.spread * 0.1  # 10% of spread, or use fixed 5.0 points
        
        for idx, pair in sorted_items:
            # RETIREMENT GUARD: Block all re-entries if pair reached TP/SL
            if pair.tp_blocked:
                continue
            
            # ================================================================
            # [SIMPLE HEDGE TRIGGER]
            # Rule: "Once a pair trades to max positions then execute hedge."
            # This is independent of completion status or any other blocks.
            # ================================================================
            if pair.trade_count >= self.max_positions and self.hedge_enabled and not pair.hedge_active:
                # Deterministic Hedge Direction Logic
                hedge_dir = None
                is_odd = (self.max_positions % 2 != 0)
                
                if idx <= 0: # Zero & Negative Pairs
                    if is_odd: hedge_dir = "sell"
                    else:      hedge_dir = "buy"
                else: # Positive Pairs
                    if is_odd: hedge_dir = "buy"
                    else:      hedge_dir = "sell"
                
                print(f" {self.symbol}: [HEDGE TRIGGER] Pair {idx} hit Max {self.max_positions}. executing {hedge_dir.upper()} hedge.")
                await self._execute_hedge(idx, hedge_dir)
                # Continue triggers to allow expansion if needed, but hedge is prioritised


            # GROUPS+CAP GATE: Only process pairs that have EVER been completed
            # (both buy_filled and sell_filled are True at some point)
            # Expansion is handled by step triggers, this is only for toggle trading
            # because toggle re-entry should work even after one leg hits TP/SL
            if not (pair.buy_filled and pair.sell_filled):
                continue
            
            # ================================================================
            # USE LOCKED ENTRY PRICES FOR RE-ENTRIES
            # Once a trade executes, its entry price is locked forever.
            # Re-entries must happen at the exact same level.
            # ================================================================
            buy_trigger = pair.locked_buy_entry if pair.locked_buy_entry > 0 else pair.buy_price
            sell_trigger = pair.locked_sell_entry if pair.locked_sell_entry > 0 else pair.sell_price
            
            # NOTE: Proximity-based re-entry for Phoenix pairs removed - no longer used
            # ================================================================
            # STANDARD DIRECTIONAL LOGIC (for normal pairs, not reopened)
            # ================================================================
            
            # --- BUY TRIGGER ---
            if idx > 0:   buy_in_zone_now = ask >= buy_trigger
            elif idx < 0: buy_in_zone_now = bid >= buy_trigger
            else:         buy_in_zone_now = ask >= buy_trigger
            
            # Zone EXIT
            if pair.buy_in_zone and not buy_in_zone_now:
                pair.buy_in_zone = False
                if pair.buy_pending_ticket == 0:
                    pair.buy_pending_ticket = self._place_pending_order(
                        self._get_order_type("buy", buy_trigger), buy_trigger, idx
                    )

            # Zone ENTRY Logic
            buy_attempt_failed = False
            
            # Zone latch only applies to FIRST trade (trade_count==0).
            # Subsequent trades (trade_count > 0) fire immediately while in zone.
            # NOTE: Reopened pairs are handled by PROXIMITY check above and won't reach here.
            if pair.trade_count > 0:
                # Immediate trigger - no leave-and-return required
                should_trigger_buy = buy_in_zone_now and pair.next_action == "buy"
            else:
                # First trade requires leave-and-return (edge detection)
                should_trigger_buy = buy_in_zone_now and not pair.buy_in_zone and pair.next_action == "buy"
            
            if should_trigger_buy:
                # FIXED: Do NOT check if pair.buy_filled here. 
                # We allow multiple buys if trade_count < max_positions.
                
                # 1. Normal Entry (Under Max Cap)
                if pair.trade_count < self.max_positions:
                    if await self._execute_trade_with_chain("buy", idx):
                        # Success - check expansion
                        indices = sorted(self.pairs.keys())
                        if idx == indices[-1] and idx >= 0:
                            await self._create_next_positive_pair(idx)
                    else:
                        buy_attempt_failed = True
                
                else:
                    # Logic block (capped)
                    pair.buy_in_zone = True 
                    # [FIX] STILL EXPAND GRID even if trade is blocked by max_positions
                    # This ensures the ladder continues up if price keeps rising
                    indices = sorted(self.pairs.keys())
                    if idx == indices[-1] and idx >= 0:
                        await self._create_next_positive_pair(idx)

            if not buy_attempt_failed and not pair.buy_in_zone:
                 pair.buy_in_zone = buy_in_zone_now

            
            # --- SELL TRIGGER ---
            if idx > 0:   sell_in_zone_now = ask <= sell_trigger
            elif idx < 0: sell_in_zone_now = bid <= sell_trigger
            else:         sell_in_zone_now = bid <= sell_trigger
            
            # Zone EXIT
            if pair.sell_in_zone and not sell_in_zone_now:
                pair.sell_in_zone = False
                if pair.sell_pending_ticket == 0:
                    pair.sell_pending_ticket = self._place_pending_order(
                        self._get_order_type("sell", sell_trigger), sell_trigger, idx
                    )

            # Zone ENTRY Logic
            sell_attempt_failed = False
            
            # Zone latch only applies to FIRST trade (trade_count==0).
            # Subsequent trades (trade_count > 0) fire immediately while in zone.
            # NOTE: Reopened pairs are handled by PROXIMITY check above and won't reach here.
            if pair.trade_count > 0:
                # Immediate trigger - no leave-and-return required
                should_trigger_sell = sell_in_zone_now and pair.next_action == "sell"
            else:
                # First trade requires leave-and-return (edge detection)
                should_trigger_sell = sell_in_zone_now and not pair.sell_in_zone and pair.next_action == "sell"
            
            if should_trigger_sell:
                # FIXED: Removed pair.sell_filled guard.
                
                # 1. Normal Entry (Under Max Cap)
                if pair.trade_count < self.max_positions:
                    if await self._execute_trade_with_chain("sell", idx):
                        # Success - check expansion
                        indices = sorted(self.pairs.keys())
                        if idx == indices[0] and idx <= 0:
                            await self._create_next_negative_pair(idx)
                    else:
                        sell_attempt_failed = True
                        
                else:
                    pair.sell_in_zone = True
                    # [FIX] STILL EXPAND GRID even if trade is blocked by max_positions
                    # This ensures the ladder continues down if price keeps falling
                    indices = sorted(self.pairs.keys())
                    if idx == indices[0] and idx <= 0:
                        await self._create_next_negative_pair(idx)

            if not sell_attempt_failed and not pair.sell_in_zone:
                pair.sell_in_zone = sell_in_zone_now

        
        # Retroactive Chain Catch-Up (Unchanged logic, just simplified check)
        if sorted_items:
            last_idx = sorted_items[-1][0]
            tick = mt5.symbol_info_tick(self.symbol)
            if tick:
                for offset in range(-2, 3):
                    check_idx = last_idx + offset
                    check_pair = self.pairs.get(check_idx)
                    
                    if not check_pair or self.trade_in_progress.get(check_idx, False):
                        continue
                    
                    # Late Chain Buy
                    if check_pair.next_action == "buy" and check_pair.trade_count < self.max_positions:
                        prev_pair = self.pairs.get(check_idx - 1)
                        if prev_pair and prev_pair.sell_filled:
                            if abs(check_pair.buy_price - prev_pair.sell_price) < 10.0:
                                if abs(tick.ask - check_pair.buy_price) < 7.0: # Freshness check
                                     await self._execute_trade_with_chain("buy", check_idx)
                    
                    # Late Chain Sell
                    if check_pair.next_action == "sell" and check_pair.trade_count < self.max_positions:
                        next_pair = self.pairs.get(check_idx + 1)
                        if next_pair and next_pair.buy_filled:
                            if abs(check_pair.sell_price - next_pair.buy_price) < 10.0:
                                if abs(tick.bid - check_pair.sell_price) < 7.0: # Freshness check
                                    await self._execute_trade_with_chain("sell", check_idx)
    
    async def _enforce_hedge_invariants(self):
        """
        HEDGE SUPERVISOR: State-based enforcement of hedge rules.
        
        This runs at the START of every tick cycle (before triggers) to ensure
        hedges are placed when required. This is more robust than trying to
        fire hedges atomically during trade execution because:
        
        1. Decoupling - Trade logic and hedge logic don't fight for resources
        2. Resilience - If hedge fails, it retries on next tick
        3. Crash-proof - On restart, supervisor sees missing hedge and fixes it
        
        Rule: If trade_count >= max_positions AND hedge_active is False -> Execute Hedge
        """
        if not self.hedge_enabled:
            return
            
        for idx, pair in self.pairs.items():
            # Check if this pair needs a hedge
            # Check if this pair needs a hedge
            if pair.trade_count >= self.max_positions and not pair.hedge_active:
                # Determine hedge direction (opposite of next_action, or based on exposure)
                # If next_action is "buy", we sold last, so hedge with buy
                # If next_action is "sell", we bought last, so hedge with sell
                hedge_direction = pair.next_action
                
                print(f" {self.symbol}: [HEDGE SUPERVISOR] Pair {idx} at max positions ({pair.trade_count}/{self.max_positions}) - Executing hedge ({hedge_direction.upper()})")
                
                success = await self._execute_hedge(idx, hedge_direction)
                
                if success:
                    print(f" {self.symbol}: [HEDGE SUPERVISOR] Hedge for Pair {idx} SUCCESSFUL")
                else:
                    print(f" {self.symbol}: [HEDGE SUPERVISOR] Hedge for Pair {idx} FAILED - will retry next tick")

    async def _execute_hedge(self, pair_index: int, direction: str) -> bool:
        """
        Execute a HEDGE order to lock the pair.
        Fixes inheritance logic and ensures TP/SL are forced to valid levels.
        """
        pair = self.pairs.get(pair_index)
        if not pair or pair.hedge_active:
            return False
            
        if not self.hedge_enabled:
            return False
            
        print(f" {self.symbol}: MAX POSITIONS ({self.max_positions}) REACHED for Pair {pair_index}. Executing HEDGE ({direction.upper()}).")
        
        tick = mt5.symbol_info_tick(self.symbol)
        sym_info = mt5.symbol_info(self.symbol)
        if not tick or not sym_info:
            return False

        point = sym_info.point
        stops_level = max(sym_info.trade_stops_level, 10) * point
        
        
        # --- 1. TRUE INHERITANCE: Find opposing position and mirror it ---
        # "Inherit from the position it's hedging"
        
        target_tp = 0.0
        target_sl = 0.0
        found_inheritance = False
        
        # Scan ticket map for the opposing leg of THIS pair index
        target_leg = 'S' if direction == 'buy' else 'B'
        
        for ticket, info in self.ticket_map.items():
            t_idx, t_leg, t_entry, t_tp, t_sl = info
            if t_idx == pair_index and t_leg == target_leg:
                # Found the position we are hedging against!
                # MIRROR LOGIC:
                # Hedge TP = Opposing SL
                # Hedge SL = Opposing TP
                target_tp = t_sl
                target_sl = t_tp
                found_inheritance = True
                print(f" {self.symbol}: [HEDGE-INHERIT] Found Opposing {target_leg} (Ticket {ticket}). Mirroring: TP={target_tp:.5f} SL={target_sl:.5f}")
                break
        
        if found_inheritance:
            h_tp = target_tp
            h_sl = target_sl
        else:
            print(f" {self.symbol}: [HEDGE-WARNING] Could not find opposing position to inherit. Using fallback calculation.")
            # Fallback Logic (Standard Grid Specs)
            if pair.pair_tp > 0 and pair.pair_sl > 0:
                 h_tp = max(pair.pair_tp, pair.pair_sl) if direction == 'buy' else min(pair.pair_tp, pair.pair_sl)
                 h_sl = min(pair.pair_tp, pair.pair_sl) if direction == 'buy' else max(pair.pair_tp, pair.pair_sl)
            else:
                 h_tp = pair.sell_price + self.spread if direction == 'buy' else pair.buy_price - self.spread # Just rough estimate
                 h_sl = pair.sell_price - self.spread if direction == 'buy' else pair.buy_price + self.spread


        # --- 3. FORCE VALIDITY (The "Push" Logic) ---
        # Instead of removing invalid stops, we push them to the nearest valid price
        
        bid = tick.bid
        ask = tick.ask
        
        if direction == "buy":
            # BUY TP Check (Must be > Ask + StopsLevel)
            min_tp = ask + stops_level
            if h_tp < min_tp:
                print(f"   [ADJ] Buy Hedge TP {h_tp:.5f} too low. Pushing to {min_tp:.5f}")
                h_tp = min_tp
                
            # BUY SL Check (Must be < Bid - StopsLevel)
            max_sl = bid - stops_level
            if h_sl > max_sl:
                print(f"   [ADJ] Buy Hedge SL {h_sl:.5f} too high. Pushing to {max_sl:.5f}")
                h_sl = max_sl

        else: # direction == "sell"
            # SELL TP Check (Must be < Bid - StopsLevel)
            max_tp = bid - stops_level
            if h_tp > max_tp:
                print(f"   [ADJ] Sell Hedge TP {h_tp:.5f} too high. Pushing to {max_tp:.5f}")
                h_tp = max_tp
                
            # SELL SL Check (Must be > Ask + StopsLevel)
            min_sl = ask + stops_level
            if h_sl < min_sl:
                print(f"   [ADJ] Sell Hedge SL {h_sl:.5f} too low. Pushing to {min_sl:.5f}")
                h_sl = min_sl

        # --- 4. EXECUTION ---
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": self.hedge_lot_size,
            "type": mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL,
            "price": ask if direction == "buy" else bid,
            "magic": 90000 + pair_index,
            "comment": f"H{pair_index} Grp{self.cycle_id}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
            "tp": float(h_tp),
            "sl": float(h_sl)
        }
        
        result = mt5.order_send(request)
        
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f" {self.symbol}: HEDGE EXECUTED for Pair {pair_index} @ {request['price']:.2f} | Ticket: {result.order}")
            
            pair.hedge_active = True
            pair.hedge_ticket = result.order
            pair.hedge_direction = direction
            
            await self._log_trade(
                event_type="HEDGE",
                pair_index=pair_index,
                direction=direction.upper(),
                price=request['price'],
                lot_size=self.hedge_lot_size,
                ticket=result.order,
                notes=f"Locked (TP={h_tp:.2f}, SL={h_sl:.2f})"
            )
            
            await self.save_state()
            return True
        
        # Log precise error for debugging
        err_desc = result.comment if result else "Unknown"
        ret_code = result.retcode if result else 0
        print(f" {self.symbol}: HEDGE FAILED for Pair {pair_index}: {err_desc} ({ret_code})")
        return False

    async def _execute_market_order(self, direction: str, price: float, index: int, reason: str = "TRADE") -> int:
        """Execute a market order and return the position ticket.
        
        TP/SL ALIGNMENT LOGIC:
        - First trade in a pair: Use UI input to set TP and SL normally
        - Second trade in same pair: Buy TP = Sell SL, Buy SL = Sell TP
        - This ensures both positions share the same exit levels
        
        GRID PRICE FIX:
        - Use the GRID price (passed as 'price' parameter) for TP/SL calculations and logging
        - This ensures B(n) and S(n) are always exactly 'spread' pips apart
        - Actual execution happens at market price, but TP/SL reference the grid level
        
        GROUPS + 3-CAP:
        - Lock gate check prevents completing a pair when C >= 3
        - Ticket mapping stored for TP detection
        """
        # GLOBAL LOCK GATE: Check if this order would violate the 3-completed cap
        leg = 'B' if direction == 'buy' else 'S'
        if not self._can_place_completing_leg(index, leg):
            return 0  # Blocked by cap
        
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return 0
        
        # GRID PRICE FIX: Use the grid price for TP/SL calculations
        # This is the intended price level, not the current market quote
        grid_price = price
        exec_price = tick.ask if direction == "buy" else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
        
        # Get volume based on pair section and fill order
        volume = self._get_lot_size(index, direction)
        
        # HARD CAP: If volume is None, pair has reached max_positions - block trade
        if volume is None:
            print(f" {self.symbol}: BLOCKED {direction.upper()} @ Pair {index} - max_positions reached")
            return 0
        
        # Get the pair to check if TP/SL levels are already set
        pair = self.pairs.get(index)
        
        # --- ROBUST TP/SL CALCULATION ---
        # Use EXECUTION PRICE (actual entry), NOT grid price, for TP/SL
        # This ensures consistent pip distance regardless of slippage/market conditions
        tp_pips = float(self.config.get(f'{direction}_stop_tp', 20.0))
        sl_pips = float(self.config.get(f'{direction}_stop_sl', 20.0))
        
        if direction == "buy":
            tp = exec_price + tp_pips
            sl = exec_price - sl_pips
        else:
            tp = exec_price - tp_pips
            sl = exec_price + sl_pips
        
        # DEBUG: Log TP/SL calculation
        #print(f"[TP/SL] {direction.upper()} Pair {index}: exec_price={exec_price:.2f} tp_pips={tp_pips} sl_pips={sl_pips} → TP={tp:.2f} SL={sl:.2f}")

        # 3. SAFETY CHECK: Validate against Current Market Price (Execution Price)
        # MT5 'Invalid Stops' happens if TP/SL are too close to CURRENT Ask/Bid
        symbol_info = mt5.symbol_info(self.symbol)
        if symbol_info:
            point = symbol_info.point
            stops_level = max(symbol_info.trade_stops_level, 10) # Minimum 10 points safety
            min_dist = stops_level * point
            
            # Validation Logic
            if direction == "buy":
                # Buy: SL must be < Bid - min_dist
                #      TP must be > Bid + min_dist (if taking profit immediately? No, TP > Ask usually)
                # Actually for Market Buy:
                # SL < Bid - StopsLevel
                # TP > Bid + StopsLevel
                
                check_price = tick.bid # Sells execute at Bid, active buys close at Bid
                
                # Enforce SL distance
                if sl > check_price - min_dist:
                    sl = check_price - min_dist
                    # print(f"   [ADJ] Buy SL adjusted to {sl:.5f} (Min Dist)")
                
                # Enforce TP distance
                if tp < check_price + min_dist:
                    tp = check_price + min_dist
                    # print(f"   [ADJ] Buy TP adjusted to {tp:.5f} (Min Dist)")
                    
            else: # Sell
                # Sell: SL must be > Ask + min_dist
                #       TP must be < Ask - min_dist
                
                check_price = tick.ask # Buys execute at Ask, active sells close at Ask
                
                # Enforce SL distance
                if sl < check_price + min_dist:
                    sl = check_price + min_dist
                    print(f"   [ADJ] Sell SL adjusted to {sl:.5f} (Min Dist)")
                    
                # Enforce TP distance
                if tp > check_price - min_dist:
                    tp = check_price - min_dist
                    print(f"   [ADJ] Sell TP adjusted to {tp:.5f} (was farther but clipped)")

        # Use cycle-aware magic and comment for TP detection
        leg = 'B' if direction == 'buy' else 'S'
        pair = self.pairs.get(index)
        trade_count = pair.trade_count if pair else 0
        magic = self.bot_magic_base + self.cycle_id
        # Human-readable: "B0 Grp1" = Buy pair 0, Group 1
        comment = f"{leg}{index} Grp{self.cycle_id}"
        
        # Place order WITH TP/SL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(volume),
            "type": order_type,
            "price": float(exec_price),
            "sl": float(sl),
            "tp": float(tp),
            "magic": magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
            "deviation": 200
        }
        
        # DEBUG: Final values sent to MT5
        print(f"[MT5-SEND] {direction.upper()} Pair {index}: exec={exec_price:.2f} TP={tp:.2f} SL={sl:.2f}")
        
        result = mt5.order_send(request)
        
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            # result.order is the ORDER ticket, NOT the POSITION ticket
            # For market orders, we need to find the actual position that was created
            # The position ticket can be found by querying positions with our magic number
            
            import time
            time.sleep(0.05)  # Small delay to ensure position is registered
            
            # Find the position we just created
            # Look for positions NOT already in ticket_map (these are new)
            positions = mt5.positions_get(symbol=self.symbol)
            position_ticket = None
            
            if positions:
                # Find positions with matching magic that aren't tracked yet
                for pos in positions:
                    if pos.magic == magic and pos.ticket not in self.ticket_map:
                        position_ticket = pos.ticket
                        break
            
            # Fallback to result.order if position not found
            if not position_ticket:
                position_ticket = result.order
                print(f"[WARNING] Could not find new position, using order ticket: {position_ticket}")
            
            # TICKET MAPPING: Store POSITION ticket with TP/SL levels for deterministic detection
            self.ticket_map[position_ticket] = (index, leg, exec_price, tp, sl)
            await self.repository.save_ticket(position_ticket, self.cycle_id, index, leg, trade_count,
                                              entry_price=exec_price, tp_price=tp, sl_price=sl)
            
            #print(f"[TICKET_MAP] pos={position_ticket} -> (cycle={self.cycle_id}, pair={index}, leg={leg})")
            
            # Log order placement
            print(f"[ORDER] cycle={self.cycle_id} pair={index} leg={leg} reason={reason}")
            
            # Log trade to history
            pair = self.pairs.get(index)
            await self._log_trade(
                event_type="OPEN",
                pair_index=index,
                direction=direction.upper(),
                price=exec_price,
                lot_size=volume,
                ticket=position_ticket,
                notes=f"TP={tp:.2f} SL={sl:.2f} C={self.cycle_id}",
                trade_count=pair.trade_count if pair else 0
            )
            
            # ================================================================
            # LOCKED ENTRY PRICES: Set ONCE on first execution, NEVER change
            # This ensures re-entries happen at the exact same price level
            # ================================================================
            if pair:
                if direction == "buy" and pair.locked_buy_entry == 0.0:
                    pair.locked_buy_entry = exec_price
                    print(f"[LOCKED] Pair {index} BUY entry locked at {exec_price:.2f}")
                elif direction == "sell" and pair.locked_sell_entry == 0.0:
                    pair.locked_sell_entry = exec_price
                    print(f"[LOCKED] Pair {index} SELL entry locked at {exec_price:.2f}")
            
            return position_ticket
        
        # Retry logic removed because we did pre-validation. 
        # If it still fails, it's a broker rejection we can't easily fix by just moving stops again blindly.
        elif result:
             print(f" {self.symbol}: Market {direction} failed: {result.comment} (RetCode: {result.retcode})")
             return 0
        
        # Final fallback - log error
        comment = result.comment if result else "Unknown error"
        print(f" {self.symbol}: Market {direction} failed: {comment}")
        return 0
    
    def _cancel_order(self, ticket: int):
        """Cancel a pending order (or ignore if virtual ticket)."""
        if not ticket or ticket < 0:
            # Virtual ticket or invalid - nothing to cancel
            return
        
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket
        }
        mt5.order_send(request)
    
    def _cancel_pair_orders(self, pair: GridPair):
        """Cancel all pending orders for a pair."""
        self._cancel_order(pair.buy_pending_ticket)
        self._cancel_order(pair.sell_pending_ticket)
        
        # Also close any open positions for this pair
        positions = mt5.positions_get(symbol=self.symbol)
        if positions:
            for pos in positions:
                if pos.magic - 50000 == pair.index:
                    self._close_position(pos)
    
    def _close_position(self, position_or_ticket):
        """Close a specific position. Accepts either position object or ticket (int)."""
        # Handle ticket (int) input - lookup position
        if isinstance(position_or_ticket, int):
            positions = mt5.positions_get(ticket=position_or_ticket)
            if not positions or len(positions) == 0:
                print(f"   [CLOSE] Position ticket={position_or_ticket} not found (already closed?)")
                return
            position = positions[0]
        else:
            position = position_or_ticket
        
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return
        
        close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        close_price = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": position.volume,
            "type": close_type,
            "position": position.ticket,
            "price": close_price,
            "deviation": 200,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"   [CLOSE] Position {position.ticket} closed successfully")
    
    # ========================================================================
    # STATE MANAGEMENT
    # ========================================================================
    
    def get_status(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "current_price": self.current_price,
            "open_positions": self.open_positions_count,
            "step": len(self.pairs),
            "iteration": self.iteration,
            "is_resetting": False,
            "phase": self.phase,
        }
    
    # ========================================================================
    # DEBUG TRADE TABLE & HISTORY
    # ========================================================================
    
    async def _log_trade(self, event_type: str, pair_index: int, direction: str, 
                   price: float, lot_size: float, ticket: int = 0, notes: str = "", trade_count: int = 0):
        """
        Log a trade event to the DB and print to console.
        """
        self.global_trade_counter += 1
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        
        event = {
            'timestamp': timestamp,
            'event_type': event_type,
            'pair_index': pair_index,
            'direction': direction,
            'price': price,
            'lot_size': lot_size,
            'ticket': ticket,
            'notes': notes
        }
        
        # Log to DB (Async)
        await self.repository.log_trade(event)
        
        # Console output
        print(f"#{self.global_trade_counter:03d} [{timestamp}] {event_type} {direction} @ {price}")
        
        # Session Logger (if exists)
        if self.session_logger:
            self.session_logger.log_trade(
                 symbol=self.symbol,
                 pair_idx=pair_index,
                 direction=direction,
                 price=price,
                 lot=lot_size,
                 trade_num=trade_count,
                 ticket=ticket
            )
    
    async def terminate(self):
        """
        Nuclear option: Close ALL positions associated with this strategy immediately.
        Fixes race conditions where positions might already be closed.
        """
        self.running = False # STOP LOGIC IMMEDIATELY
        print(f"[TERMINATE] {self.symbol}: Closing ALL positions immediately...")
        
        # 1. Cancel all pending orders first
        try:
            orders = mt5.orders_get(symbol=self.symbol)
            if orders:
                for order in orders:
                    if self.bot_magic_base <= order.magic < self.bot_magic_base + 100000:
                        request = {
                            "action": mt5.TRADE_ACTION_REMOVE,
                            "order": order.ticket
                        }
                        mt5.order_send(request)
        except Exception as e:
            print(f"[TERMINATE] Error canceling orders: {e}")

        # 2. Close all open positions
        # Use a localized list to avoid re-fetching mid-loop if possible, 
        # but re-fetching is safer for validity check.
        positions = mt5.positions_get(symbol=self.symbol)
        closed_count = 0
        if positions:
            for pos in positions:
                # Check ownership
                if hasattr(self, 'bot_manager') and self.bot_manager:
                     if not (self.bot_manager.magic_base <= pos.magic < self.bot_manager.magic_base + 100000):
                         continue
                
                # Double-check existence (Atomic-ish)
                check_pos = mt5.positions_get(ticket=pos.ticket)
                if not check_pos:
                    continue
                
                tick = mt5.symbol_info_tick(self.symbol)
                if not tick:
                    continue
                
                close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                close_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
                
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": self.symbol,
                    "volume": pos.volume,
                    "type": close_type,
                    "position": pos.ticket,
                    "price": close_price,
                    "deviation": 50,
                    "magic": pos.magic,
                    "comment": "Terminate",
                }
                
                result = mt5.order_send(request)
                if result:
                    if result.retcode == mt5.TRADE_RETCODE_DONE:
                        print(f"   [CLOSE] Position {pos.ticket} closed successfully")
                        closed_count += 1
                    elif result.retcode == mt5.TRADE_RETCODE_POSITION_CLOSED:
                        pass # Already closed, ignore
                    elif result.retcode == 10005: # INVALID_REQUEST (often means position invalid)
                        pass
                    else:
                        # Only log real errors
                        print(f"[ERROR] Failed to close position {pos.ticket}: {result.comment} ({result.retcode})")
        
        print(f"[TERMINATE] {self.symbol}: Closed {closed_count}/{len(positions) if positions else 0} positions.")
        
        # 3. Clear State
        self.pairs = {}
        self.ticket_map = {}
        self.grid_truth = None 
        
        try:
            await self.repository.reset()
            print(f"[TERMINATE] {self.symbol}: Grid reset complete.")
        except Exception as e:
            print(f"[TERMINATE] Could not clean DB: {e}")

    def print_grid_table(self):
        """
        Print detailed table of grid state, Consolidated for all groups.
        Format: Fixed 7 rows per group showing sequence of legs (B0, S1, etc.)
        Includes Event Log at the bottom.
        """
        if not self.pairs:
            print(f"\n {self.symbol}: Grid is empty\n")
            return

        buffer = []
        buffer.append(f"\n{'='*100}")
        buffer.append(f" SYMBOL: {self.symbol:<10}  PRICE: {self.current_price:<10.2f}  GROUP: {self.current_group:<3}")
        buffer.append(f"{'='*100}")

        present_groups = sorted(list(set(p.group_id for p in self.pairs.values())), reverse=True)
        
        for group_id in present_groups:
            # 1. Determine Group Direction & Sequence
            # Default sequence (pairs indices): 0, 1, 2, 3...
            # We map "Step 1..7" to specific Pair + Leg
            # Assuming standard expansion (atomic pairs) + 1 non-atomic
            
            # Retrieve intent from logger if possible, else infer
            init_direction = self.group_logger.get_init_direction(group_id) or "BULLISH" # Default
            pending_retracement = self.group_logger.get_pending_retracement(group_id)
            
            direction_label = f"{init_direction} INIT"
            if pending_retracement:
                direction_label += f" | Retrace: {pending_retracement}"
                
            buffer.append(f"\n [GROUP {group_id}] {direction_label}")
            buffer.append(f"{'-'*100}")
            buffer.append(f"{'Seq':^5} | {'Leg':^6} | {'Status':^10} | {'P/L':^8} | {'Entry':^10} | {'TP':^10} | {'SL':^10} | {'Lot':^6} | {'Notes':^15}")
            buffer.append(f"{'-'*100}")
            
            # --- GENERATE 7 ROWS ---
            # We construct the "Ideal" sequence of 7 actions (Legs)
            # This depends on logic. Assuming Standard "Grid V3" pattern:
            # Init: Leg1, Leg2
            # Exp1: Leg3, Leg4
            # Exp2: Leg5, Leg6
            # Final: Leg7
            
            # We need to map these logical steps to actual Pair Indices + Direction
            # Bullish Init (Group 0 example):
            # 1. B0
            # 2. S1
            # 3. B1 (Exp 1 - Retrace)
            # 4. S2 (Exp 1 - Trend)
            # 5. B2
            # 6. S3
            # 7. B3? Or just S3 completion? 
            # Actually, standard grid is pairs. 
            # Pair 0: B0, S0. Pair 1: B1, S1.
            # "B0, S1..." implies crossing pairs.
            
            # Simplified approach: List ALL pairs in this group (0, 1, 2, 3...)
            # For each pair, show Buy and Sell legs? That's 2 legs per pair.
            # 3 pairs = 6 legs. + 1 = 7.
            # This matches "7 positions".
            
            # Get pairs for this group
            # We assume indices are sequential: Anchor, Anchor+1, Anchor+2... 
            # (or -1, -2 for Bearish?)
            # Let's collect actual pairs and pad.
            
            group_pairs = [p for p in self.pairs.values() if p.group_id == group_id]
            # Sort by index
            # If Bullish Init: 0, 1, 2...
            # If Bearish Init: 0, -1, -2... (Check indices)
            group_pairs.sort(key=lambda x: x.index) 
            
            # We need to display 7 legs.
            # Let's iterate the standard "Ladder" sequence
            # For visualization, just listing the Pairs found + "Pending" slots is safest.
            
            # Display Order:
            # If Bullish Init: Pair 0 (B), Pair 1 (S), Pair 1 (B), Pair 2 (S)...
            # This is complex to hardcode perfectly without deeper logic knowledge.
            # User request: "all the 7 positions... B0, S1..."
            
            # I will render the pairs that SHOULD exist for a complete group.
            # A group typically has pairs: [0, 1, 2, 3] (indices relative to group start)
            
            # Let's list the known pairs + projections.
            # If we have 3 pairs, max is 4?
            # Assuming max_pairs=7 refers to 7 LEGS or 4 PAIRS (8 legs)?
            # "7 positions that make a group complete" -> C=3 (completion count).
            # C=3 usually implies 3 full pairs + 1 incomplete.
            
            # We will list:
            # Pair 0 (Buy & Sell)
            # Pair 1 (Buy & Sell)
            # Pair 2 (Buy & Sell)
            # Pair 3 (Buy & Sell)
            # ... up to limit.
            
            # But user emphasized SEQUENCE.
            # Sequence:
            # 1. Init: B0
            # 2. Init: S1
            # 3. Exp1: B1
            # 4. Exp1: S2
            # 5. Exp2: B2
            # 6. Exp2: S3
            # 7. Final: B3 (or S0 if bearish?)
            
            # I'll try to reconstruct this sequence using `group_pairs`.
            # Indices:
            # Bearish Init (Group 1 example?): S0, B-1...
            
            # Helper to print row
            def get_row_data(pair, leg_type):
                if not pair:
                    return ("EMPTY", "-", 0, 0, 0, 0, "-")
                
                status = "OPEN"
                pnl = 0.0
                entry = 0.0
                tp = 0.0
                sl = 0.0
                ticket = 0
                
                if leg_type == "BUY":
                    if pair.buy_filled: 
                        # Check if closed
                        # Simplification: If filled but no active ticket -> Closed
                        # Need to check if OPEN
                        pass # Complex without ticket lookup
                        status = "FILLED"
                        entry = pair.buy_price
                        # Try to find active ticket details
                        if pair.buy_ticket:
                            info = self.ticket_map.get(pair.buy_ticket)
                            if info: 
                                entry, tp, sl = info[2], info[3], info[4]
                                pnl = (self.current_price - entry)
                                status = "OPEN"
                    elif pair.buy_pending_ticket:
                        status = "PENDING"
                        entry = pair.buy_price
                    else:
                        status = "WAITING"
                        entry = pair.buy_price
                        
                else: # SELL
                    if pair.sell_filled:
                        status = "FILLED"
                        entry = pair.sell_price
                        if pair.sell_ticket:
                            info = self.ticket_map.get(pair.sell_ticket)
                            if info:
                                entry, tp, sl = info[2], info[3], info[4]
                                pnl = (entry - self.current_price)
                                status = "OPEN"
                    elif pair.sell_pending_ticket:
                        status = "PENDING"
                        entry = pair.sell_price
                    else:
                        status = "WAITING"
                        entry = pair.sell_price
                
                # Check "retired" (TP/SL hit)
                if status == "FILLED" and not pnl: # Rough check for closed
                     status = "CLOSED"
                
                return (status, f"{leg_type}", pnl, entry, tp, sl, f"{pair.trade_count}")

            # Construct sequence based on Init Direction
            # We assume indices 0, 1, 2... relative to anchor are mapped.
            # But pairs have absolute indices. 
            # Let's just sort pairs by index and interleave.
            
            # This is hard to perfect blindly. I will print ALL legs of ALL pairs in the group.
            # That covers everything.
            
            rows = []
            for i, pair in enumerate(group_pairs):
                # Row for Buy
                s, l, p, e, t, sl, n = get_row_data(pair, "BUY")
                rows.append(f"{i*2+1:^5} | {f'B{pair.index}':^6} | {s:^10} | {p:^8.2f} | {e:^10.2f} | {t:^10.2f} | {sl:^10.2f} | {'-':^6} | {n:^15}")
                
                # Row for Sell
                s, l, p, e, t, sl, n = get_row_data(pair, "SELL")
                rows.append(f"{i*2+2:^5} | {f'S{pair.index}':^6} | {s:^10} | {p:^8.2f} | {e:^10.2f} | {t:^10.2f} | {sl:^10.2f} | {'-':^6} | {n:^15}")
            
            # Filler for 7 positions (if less than 8 rows)
            # This is a bit hacky but meets the "show 7 positions" visual requirement
            while len(rows) < 7:
                seq = len(rows) + 1
                rows.append(f"{seq:^5} | {'?':^6} | {'EMPTY':^10} | {'-':^8} | {'-':^10} | {'-':^10} | {'-':^10} | {'-':^6} | {'-':^15}")
            
            buffer.extend(rows[:7]) # Limit to 7 if user insists? Or show all. showing all is safer.
            if len(rows) > 7:
                buffer.extend(rows[7:])

        buffer.append(f"{'='*100}")
        
        # --- ACTIVITY LOG ---
        buffer.append("\n [ACTIVITY LOG]")
        buffer.append(f"{'-'*100}")
        # Fetch logs from GroupLogger (which are nicely formatted)
        # We need to access private _get_or_create? No, group_logger has `groups`.
        if self.group_logger:
             for gid in sorted(self.group_logger.groups.keys()):
                 group = self.group_logger.groups[gid]
                 for event in group.events:
                     # Format: Time | Type | Message
                     buffer.append(f" {event['time']} | {event['type']:<15} | {event['message']}")
        
        buffer.append(f"{'='*100}\n")
        
        full_content = "\n".join(buffer)
        
        # Write to Single File
        # Use a fixed name for the session or append? "groups_table_SESSIONID.txt" is good.
        # We use the group_logger's logic to get path but override name.
        if self.group_logger:
             self.group_logger.write_raw_group_table("ALL", full_content)
        
        # Print to console
        print(full_content)
    
    def print_trade_history(self, last_n: int = 20):
        """
        Print the last N trade events in chronological order.
        """
        print(f"\n{'='*100}")
        print(f" TRADE HISTORY - {self.symbol} (Last {last_n} events)")
        print(f"{'='*100}")
        print(f"{'#':>4} {'Time':<12} {'Event':<14} {'Pair':>5} {'Dir':<5} {'Price':>12} {'Lot':>6} {'#':>3} {'Notes':<20}")
        print(f"{'-'*100}")
        
        history_slice = self.trade_history[-last_n:] if len(self.trade_history) > last_n else self.trade_history
        
        for i, log in enumerate(history_slice):
            start_idx = len(self.trade_history) - len(history_slice)
            print(f"{start_idx + i + 1:>4} {log.timestamp:<12} {log.event_type:<14} {log.pair_index:>5} {log.direction:<5} {log.price:>12.2f} {log.lot_size:>6.2f} {log.trade_num:>3} {log.notes:<20}")
        
        print(f"{'='*100}\n")
    
    def export_trade_history_to_file(self):
        """Export full trade history to a detailed log file."""
        filename = f"trade_history_{self.symbol.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        try:
            with open(filename, "w") as f:
                f.write(f"TRADE HISTORY - {self.symbol}\n")
                f.write(f"Exported: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Total Events: {len(self.trade_history)}\n")
                f.write(f"{'='*100}\n\n")
                
                for i, log in enumerate(self.trade_history):
                    f.write(f"#{i+1:03d} {log}\n")
                
                f.write(f"\n{'='*100}\n")
                f.write("GRID CONFIG:\n")
                f.write(f"  Lot Sizes: {self.lot_sizes}\n")
                f.write(f"  Spread: {self.spread}\n")
                f.write(f"  Max Pairs: {self.max_pairs}\n")
                f.write(f"  Max Positions: {self.max_positions}\n")
            
            print(f" Exported trade history to: {filename}")
            return filename
        except Exception as e:
            print(f" Failed to export: {e}")
            return None

    
    async def save_state(self):
        """Persist grid state to SQLite including cycle management."""
        # Prepare metadata blob
        metadata_dict = {
            "group_direction": self.group_direction
        }
        metadata_json = json.dumps(metadata_dict)

        await self.repository.save_state(
            self.phase, self.center_price, self.iteration,
            self.cycle_id, self.anchor_price,
            metadata=metadata_json
        )
        
        # Save All Pairs
        for pair in self.pairs.values():
            await self.repository.upsert_pair(asdict(pair))

    async def load_state(self):
        """Load grid state from SQLite."""
        state = await self.repository.get_state()
        if not state:
            print(f" {self.symbol}: No saved state found.")
            return

        self.phase = state.get('phase', self.PHASE_INIT)
        self.center_price = state.get('center_price', 0.0)
        self.iteration = state.get('iteration', 1)
        
        # Load cycle management fields
        self.cycle_id = state.get('cycle_id', 0)
        self.anchor_price = state.get('anchor_price', 0.0)
        
        # Load metadata
        metadata_json = state.get('metadata', '{}')
        try:
            metadata = json.loads(metadata_json)
            self.group_direction = metadata.get('group_direction')
        except Exception:
            self.group_direction = None
        
        # Restore last deal check time to prevent missing deals
        last_update_str = state.get('last_update_time')
        if last_update_str is not None:
            # FIX: Handle case where DB returns float timestamp directly
            if isinstance(last_update_str, (float, int)):
                self.last_deal_check_time = float(last_update_str)
            else:
                try:
                    # SQLite CURRENT_TIMESTAMP is 'YYYY-MM-DD HH:MM:SS'
                    # Try handling both strict format and potential variations
                    dt = datetime.strptime(str(last_update_str), "%Y-%m-%d %H:%M:%S")
                    self.last_deal_check_time = dt.timestamp()
                except ValueError:
                     try:
                         # Attempt with microseconds if present
                         dt = datetime.strptime(str(last_update_str), "%Y-%m-%d %H:%M:%S.%f")
                         self.last_deal_check_time = dt.timestamp()
                     except Exception:
                         pass
        
        # Load Pairs
        pair_rows = await self.repository.get_pairs()
        self.pairs = {}
        for row in pair_rows:
            idx = row['pair_index']
            pair = GridPair(
                index=idx,
                buy_price=row['buy_price'],
                sell_price=row['sell_price']
            )
            # Restore state
            pair.buy_ticket = row['buy_ticket']
            pair.sell_ticket = row['sell_ticket']
            pair.buy_filled = bool(row['buy_filled'])
            pair.sell_filled = bool(row['sell_filled'])
            pair.buy_pending_ticket = row['buy_pending_ticket']
            pair.sell_pending_ticket = row['sell_pending_ticket']
            pair.trade_count = row['trade_count']
            pair.next_action = row['next_action']
            pair.is_reopened = bool(row['is_reopened'])
            pair.buy_in_zone = bool(row['buy_in_zone'])
            pair.sell_in_zone = bool(row['sell_in_zone'])
            pair.tp_blocked = bool(row.get('tp_blocked', False))
            
            # ===== ROBUST STATE SYNCHRONIZATION =====
            # Enforce invariants regardless of what was saved in DB.
            # This prevents race conditions after crashes/restarts.
            
            # 1. FIX NEGATIVE PAIR RACE: Always latch zone if filled
            #    If position is filled, zone MUST be latched to prevent re-trigger
            if pair.buy_filled:
                pair.buy_in_zone = True
            if pair.sell_filled:
                pair.sell_in_zone = True
            
            # 2. FIX POSITIVE PAIR WRONG DIRECTION: Sync toggle with fill state
            #    If we have a sell but no buy (or last was sell), next must be buy
            if pair.sell_filled and not pair.buy_filled:
                if pair.next_action != "buy":
                    print(f"[SYNC] {self.symbol} Pair {idx}: sell_filled but next_action was '{pair.next_action}' - correcting to 'buy'")
                    pair.next_action = "buy"
            elif pair.buy_filled and not pair.sell_filled:
                if pair.next_action != "sell":
                    print(f"[SYNC] {self.symbol} Pair {idx}: buy_filled but next_action was '{pair.next_action}' - correcting to 'sell'")
                    pair.next_action = "sell"
            
            # 3. SANITY CHECK: Repair trade_count if 0 but filled
            #    If pair is filled but trade_count is 0, the DB was saved inconsistently
            if (pair.buy_filled or pair.sell_filled) and pair.trade_count == 0:
                print(f"[SANITY] {self.symbol} Pair {idx}: Filled but trade_count=0 - correcting to trade_count=1")
                pair.trade_count = 1
            
            # ===== END STATE SYNCHRONIZATION =====
            
            self.pairs[idx] = pair
            # Update ground truth
            self.grid_truth.add_level(pair.buy_price, pair.sell_price, idx)
            
        print(f" {self.symbol}: Loaded state (Phase={self.phase}, Pairs={len(self.pairs)})")

    # ========================================================================
    # GROUP TRANSITION HELPERS
    # ========================================================================
    def _is_group_init_triggered(self, group_id: int) -> bool:
        """Check if INIT for a specific group has already been fired."""
        # Use lazy initialization for backward compatibility with state loading
        if not hasattr(self, '_triggered_groups'):
             self._triggered_groups = set()
        return group_id in self._triggered_groups

    def _mark_group_init_triggered(self, group_id: int):
        """Mark a group as triggered to prevent duplicate INIT calls."""
        if not hasattr(self, '_triggered_groups'):
             self._triggered_groups = set()
        self._triggered_groups.add(group_id)


# Alias for backward compatibility
GridStrategy = SymbolEngine
