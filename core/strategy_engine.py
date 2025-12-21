"""
Infinite Ladder Grid Strategy (LadderGridStrategy)

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
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, List, Any
from collections import defaultdict
import MetaTrader5 as mt5
from datetime import datetime


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


@dataclass
class GridPair:
    """
    Represents a Buy/Sell pair at a specific grid level.
    
    Each pair has its own "brain" / memory:
    - trade_count: How many trades executed for THIS pair
    - next_action: Toggle state for buy→sell→buy sequence
    - Lot formula: T[i] = L[i] (Trade #N uses Lot #N)
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
    trade_count: int = 0            # Number of trades executed in THIS pair
    next_action: str = "buy"        # Toggle: "buy" → "sell" → "buy" → ...
    first_fill_direction: str = ""  # Legacy: "buy" or "sell" - whichever filled first
    
    # Zone tracking for re-trigger (price must LEAVE and RETURN to re-trigger)
    buy_in_zone: bool = False       # True if price is currently at buy trigger level
    sell_in_zone: bool = False      # True if price is currently at sell trigger level
    
    # TP/SL Alignment: First trade of each 2-trade cycle sets these, second trade uses them inversely
    # Buy TP = Sell SL, Buy SL = Sell TP (determined by trade_count % 2)
    pair_tp: float = 0.0            # Shared TP level (set by first trade of cycle)
    pair_sl: float = 0.0            # Shared SL level (set by first trade of cycle)
    
    def get_next_lot(self, lot_sizes: list) -> float:
        """
        T[i] = L[i] - Trade #N uses Lot Size #N
        Cycles if trade_count exceeds lot_sizes length.
        """
        if not lot_sizes:
            return 0.01
        idx = self.trade_count % len(lot_sizes)
        return float(lot_sizes[idx])
    
    def advance_toggle(self):
        """Advance to next action in toggle sequence"""
        self.trade_count += 1
        self.next_action = "sell" if self.next_action == "buy" else "buy"
    

class LadderGridStrategy:
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
    
    def __init__(self, config_manager, symbol: str, session_logger=None):
        self.config_manager = config_manager
        self.symbol = symbol
        self.session_logger = session_logger
        self.running = False
        
        # --- Grid State ---
        self.phase = self.PHASE_INIT
        self.center_price: float = 0.0          # Anchor price (adjusts when first fill happens)
        self.pairs: Dict[int, GridPair] = {}    # Active pairs keyed by index
        self.iteration: int = 1                 # Cycle count
        
        # --- Tracking ---
        self.current_price: float = 0.0
        self.open_positions_count: int = 0
        self.pending_orders_count: int = 0
        self.start_time: float = 0
        self.is_busy: bool = False              # Lock for order operations
        
        # --- Auto-restart tracking ---
        self.last_trade_time: float = 0         # Last time we had active trades
        self.no_trade_timeout: float = 10.0    # Seconds before auto-restart (10 seconds)
        
        # --- Debug Trade History ---
        self.trade_history: List[TradeLog] = []          # All trade events in order
        self.global_trade_counter: int = 0               # Total trades across all pairs
        self.debug_log_file = f"trade_debug_{self.symbol.replace(' ', '_')}.txt"
        
        # --- Graceful Stop ---
        self.graceful_stop: bool = False    # When True, complete open pairs before stopping
        
        # --- History-Based TP/SL Detection ---
        self.last_deal_check_time: float = time.time()  # Track last history query time
        
        # --- Persistence ---
        self.state_file = f"ladder_state_{self.symbol.replace(' ', '_')}.json"
        
        self.load_state()
        
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
    
    # ========================================================================
    # LIFECYCLE
    # ========================================================================
    
    async def start_ticker(self):
        """Called when config updates."""
        print(f" {self.symbol}: Config Updated.")
        # Could trigger re-validation of grid if spread changed significantly
        pass
    
    async def start(self):
        self.running = True
        self.start_time = time.time()
        
        if not mt5.symbol_select(self.symbol, True):
            print(f" {self.symbol}: Failed to select symbol in MT5.")
            return
        
        # ALWAYS reset to INIT phase for fresh B0 execution
        # This ensures B0 BUY is executed immediately when Start is clicked
        self.phase = self.PHASE_INIT
        self.pairs = {}  # Clear any old pairs
        self.center_price = 0.0
        self.iteration = 1
        
        # Delete old state file to prevent stale data
        if os.path.exists(self.state_file):
            os.remove(self.state_file)
            print(f" {self.symbol}: Cleared old state file.")
        
        print(f"[START] {self.symbol}: LadderGridStrategy Started (Fresh Start - B0 will execute)")
    
    async def stop(self):
        """
        Graceful stop - sets flag to complete open pairs to max_positions before stopping.
        """
        print(f"[STOP] {self.symbol}: Graceful stop initiated. Completing open pairs...")
        self.graceful_stop = True
        # Don't set self.running = False here; let _check_graceful_stop_complete handle it
        self.save_state()
    
    def _check_graceful_stop_complete(self) -> bool:
        """
        Check if graceful stop is complete (all open pairs at max_positions).
        Returns True if we should fully stop now.
        """
        if not self.graceful_stop:
            return False
        
        # Check each pair that has any trades
        for idx, pair in self.pairs.items():
            # If this pair has any active positions (buy or sell filled)
            if pair.buy_filled or pair.sell_filled:
                if pair.trade_count < self.max_positions:
                    # Still has trades to complete
                    return False
        
        # All active pairs have reached max_positions - fully stop now
        self.running = False
        self.graceful_stop = False
        print(f"[STOP] {self.symbol}: Graceful stop complete. All pairs at max_positions.")
        self.save_state()
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
        
        # Clear state file
        if os.path.exists(self.state_file):
            os.remove(self.state_file)
        
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
        if self.graceful_stop and self._check_graceful_stop_complete():
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
        INIT: Execute B0 IMMEDIATELY at market price.
        
        On Start:
        - B0 executes immediately at market (ask)
        - S0 is set to B0 - spread (20 pips below B0)
        - B-1 is at the SAME level as S0 (so they trigger together)
        
        LADDER STRUCTURE (with spread=20):
        B0 @ 100 (IMMEDIATE at market)
        S0 @ 80  (trigger when price drops 20)
        B-1 @ 80 (same level as S0, triggers together)
        S-1 @ 60 (20 below B-1)
        """
        # B0 price = current ask (immediate execution)
        b0_price = ask
        self.center_price = b0_price
        
        # Create center pair (0)
        pair = GridPair(
            index=0,
            buy_price=b0_price,              # B0 = market price
            sell_price=b0_price - self.spread  # S0 = B0 - spread
        )
        self.pairs[0] = pair
        
        # Execute B0 IMMEDIATELY at market
        print(f" {self.symbol}: Executing B0 IMMEDIATELY @ {b0_price:.2f}")
        ticket = self._execute_market_order("buy", b0_price, 0)
        if ticket:
            pair.buy_filled = True
            pair.buy_ticket = ticket
            pair.buy_in_zone = True  # Mark as in zone
            pair.advance_toggle()  # Advance to next action (sell)
            self.last_trade_time = time.time()  # Start the auto-restart timer
            
            # Set up virtual S0 trigger (S0 is BELOW B0, so it's a sell_stop)
            pair.sell_pending_ticket = self._place_pending_order(
                "sell_stop", pair.sell_price, 0
            )
            
            print(f"   B0 FILLED @ {b0_price:.2f}, S0 pending @ {pair.sell_price:.2f}")
            
            # Now create pair -1 with B-1 at same level as S0
            if self.max_pairs >= 3:
                pair_minus1 = GridPair(
                    index=-1,
                    buy_price=pair.sell_price,  # B-1 = S0 (same trigger level)
                    sell_price=pair.sell_price - self.spread  # S-1 = B-1 - spread
                )
                # NEGATIVE pairs: BUY triggers first
                pair_minus1.next_action = "buy"
                self.pairs[-1] = pair_minus1
                # ARM BOTH triggers for pair -1
                pair_minus1.buy_pending_ticket = self._place_pending_order(
                    "buy_limit", pair_minus1.buy_price, -1
                )
                pair_minus1.sell_pending_ticket = self._place_pending_order(
                    "sell_stop", pair_minus1.sell_price, -1
                )
                print(f"   B-1 @ {pair_minus1.buy_price:.2f} (same as S0), S-1 @ {pair_minus1.sell_price:.2f} [next=BUY]")
            
            # Skip to EXPANDING phase (B0 already filled)
            self.phase = self.PHASE_EXPANDING
            print(f" {self.symbol}: Grid Initialized. B0 FILLED, proceeding to expand grid.")
        else:
            print(f" {self.symbol}: Failed to execute B0. Retrying...")
        
        self.save_state()
    
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
                ticket = self._execute_market_order("buy", pair.buy_price, pair.index)
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
                    
                    self.save_state()
        
        # Check if S1 filled (Bid reached Sell Stop price)
        if not pair.sell_filled:
            if bid <= pair.sell_price:
                # S1 triggered! Execute market sell
                ticket = self._execute_market_order("sell", pair.sell_price, pair.index)
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
                    
                    self.save_state()
        
        # Check if BOTH filled -> transition
        if pair.buy_filled and pair.sell_filled:
            self.center_price = (pair.buy_price + pair.sell_price) / 2
            self.phase = self.PHASE_EXPANDING
            print(f" {self.symbol}: Center Pair Complete. Expanding Grid...")
            self.save_state()
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
            self.save_state()
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
            self.save_state()
    
    async def _handle_expanding(self, ask: float, bid: float):
        """
        EXPANDING: Create ALL pairs up to max_level.
        
        max_pairs=3 → max_level=1 → pairs: -1, 0, +1
        max_pairs=5 → max_level=2 → pairs: -2, -1, 0, +1, +2
        max_pairs=7 → max_level=3 → pairs: -3, -2, -1, 0, +1, +2, +3
        """
        center_pair = self.pairs.get(0)
        if not center_pair:
            self.phase = self.PHASE_INIT
            return
        
        max_level = (self.max_pairs - 1) // 2
        
        # Create positive pairs (1, 2, 3, ...) up to max_level
        for level in range(1, max_level + 1):
            if level not in self.pairs:
                # Use the previous pair as reference
                ref_pair = self.pairs.get(level - 1)
                if ref_pair:
                    await self._create_expansion_pair(level, ref_pair, ask, bid)
        
        # Create negative pairs (-1, -2, -3, ...) down to -max_level
        for level in range(-1, -max_level - 1, -1):
            if level not in self.pairs:
                # Use the previous pair (less negative) as reference
                ref_pair = self.pairs.get(level + 1)
                if ref_pair:
                    await self._create_expansion_pair(level, ref_pair, ask, bid)
        
        # Transition to RUNNING
        self.phase = self.PHASE_RUNNING
        print(f" {self.symbol}: Grid expanded to {len(self.pairs)} pairs (max_level={max_level}). Running...")
        
        # Print grid table for debug visualization
        self.print_grid_table()
        
        self.save_state()
    
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
        RUNNING: Monitor virtual triggers and check for TP/SL re-opens.
        Also auto-restart if no active trades for 5+ seconds.
        """
        # Check for active positions
        positions = mt5.positions_get(symbol=self.symbol)
        active_count = len(positions) if positions else 0
        
        if active_count > 0:
            # We have active trades - update last trade time
            self.last_trade_time = time.time()
        else:
            # No active trades - check timeout for auto-restart
            if self.last_trade_time > 0:
                elapsed = time.time() - self.last_trade_time
                if elapsed >= self.no_trade_timeout:
                    self.iteration += 1
                    print(f" {self.symbol}: No active trades for {elapsed:.1f}s - RESTARTING cycle #{self.iteration}")
                    
                    # Reset to INIT phase (will buy B0 at market on next tick)
                    self.phase = self.PHASE_INIT
                    self.pairs = {}
                    self.center_price = 0.0
                    self.last_trade_time = 0
                    
                    # Clear old state file
                    if os.path.exists(self.state_file):
                        os.remove(self.state_file)
                    
                    self.save_state()
                    return  # Exit, next tick will run _handle_init
        
        # [FIX] Check TP/SL from MT5 history FIRST (before triggers/reopen)
        await self._check_tp_sl_from_history()
        
        # 1. Check virtual triggers (monitor prices and fire market orders)
        self._check_virtual_triggers(ask, bid)
        
        # 2. Check for closed positions (TP/SL hit) and re-open
        await self._check_and_reopen()
    
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
                    self.save_state()
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
                            self.save_state()
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
                            self.save_state()
                            return
    

    async def _check_tp_sl_from_history(self):
        """
        [FIX] History-based TP/SL detection using MT5 deal history.
        Queries authoritative MT5 records to detect when TP or SL was hit.
        This eliminates race conditions from snapshot-based detection.
        """
        try:
            # Query deals since last check
            from_time = datetime.fromtimestamp(self.last_deal_check_time)
            deals = mt5.history_deals_get(from_time, datetime.now(), symbol=self.symbol)
            
            if not deals:
                # No new deals or query failed
                return
            
            for deal in deals:
                # Check if this was a TP or SL closure
                if deal.reason == mt5.DEAL_REASON_TP:
                    reason = "TP"
                elif deal.reason == mt5.DEAL_REASON_SL:
                    reason = "SL"
                else:
                    continue  # Not a TP/SL close, skip
                
                # Map deal to pair using magic number
                if deal.magic < 50000:
                    continue  # Not our order
                
                pair_idx = deal.magic - 50000
                pair = self.pairs.get(pair_idx)
                
                if not pair:
                    continue  # Pair no longer exists
                
                print(f"[{reason}_HIT] {self.symbol}: Pair {pair_idx} - Position {deal.position_id} closed")
                
                # Log to session
                if self.session_logger:
                    self.session_logger.log_tp_sl(
                        symbol=self.symbol,
                        pair_idx=pair_idx,
                        direction="BUY" if deal.type == mt5.DEAL_TYPE_BUY else "SELL",
                        result="tp" if reason == "TP" else "sl",
                        profit=deal.profit
                    )
                
                # [CRITICAL FIX] Reset trade count to 0
                old_count = pair.trade_count
                pair.trade_count = 0
                print(f"   [RESET] Pair {pair_idx} trade_count reset to 0 (was {old_count})")
                
                # Nuclear reset: Close opposite side if still open
                if deal.type == mt5.DEAL_TYPE_SELL:  # Closed a BUY position
                    pair.buy_filled = False
                    pair.buy_ticket = 0
                    
                    # Close opposite SELL if open
                    if pair.sell_filled and pair.sell_ticket:
                        print(f"   [PAIR RESET] Closing opposite Sell {pair.sell_ticket}...")
                        # Get position and close it properly
                        sell_pos = mt5.positions_get(ticket=pair.sell_ticket)
                        if sell_pos:
                            pos = sell_pos[0]
                            tick = mt5.symbol_info_tick(self.symbol)
                            if tick:
                                close_request = {
                                    "action": mt5.TRADE_ACTION_DEAL,
                                    "symbol": self.symbol,
                                    "position": pair.sell_ticket,
                                    "volume": pos.volume,
                                    "type": mt5.ORDER_TYPE_BUY,  # Buy to close sell
                                    "price": tick.ask,
                                    "deviation": 50,
                                    "magic": pos.magic,
                                    "comment": "Nuclear Reset (TP/SL)",
                                }
                                result = mt5.order_send(close_request)
                                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                                    print(f"   [SUCCESS] Closed Sell {pair.sell_ticket}")
                                else:
                                    print(f"   [FAILED] Could not close Sell {pair.sell_ticket}")
                        pair.sell_filled = False
                        pair.sell_ticket = 0
                
                elif deal.type == mt5.DEAL_TYPE_BUY:  # Closed a SELL position
                    pair.sell_filled = False
                    pair.sell_ticket = 0
                    
                    # Close opposite BUY if open
                    if pair.buy_filled and pair.buy_ticket:
                        print(f"   [PAIR RESET] Closing opposite Buy {pair.buy_ticket}...")
                        # Get position and close it properly
                        buy_pos = mt5.positions_get(ticket=pair.buy_ticket)
                        if buy_pos:
                            pos = buy_pos[0]
                            tick = mt5.symbol_info_tick(self.symbol)
                            if tick:
                                close_request = {
                                    "action": mt5.TRADE_ACTION_DEAL,
                                    "symbol": self.symbol,
                                    "position": pair.buy_ticket,
                                    "volume": pos.volume,
                                    "type": mt5.ORDER_TYPE_SELL,  # Sell to close buy
                                    "price": tick.bid,
                                    "deviation": 50,
                                    "magic": pos.magic,
                                    "comment": "Nuclear Reset (TP/SL)",
                                }
                                result = mt5.order_send(close_request)
                                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                                    print(f"   [SUCCESS] Closed Buy {pair.buy_ticket}")
                                else:
                                    print(f"   [FAILED] Could not close Buy {pair.buy_ticket}")
                        pair.buy_filled = False
                        pair.buy_ticket = 0
                
                # Reset flags
                pair.buy_in_zone = False
                pair.sell_in_zone = False
                pair.first_fill_direction = ""
                
                # Cancel any existing pending orders
                if pair.buy_pending_ticket:
                    self._cancel_order(pair.buy_pending_ticket)
                if pair.sell_pending_ticket:
                    self._cancel_order(pair.sell_pending_ticket)
                
                # Re-place triggers for both sides
                pair.buy_pending_ticket = self._place_pending_order(
                    self._get_order_type("buy", pair.buy_price),
                    pair.buy_price, pair_idx
                )
                pair.sell_pending_ticket = self._place_pending_order(
                    self._get_order_type("sell", pair.sell_price),
                    pair.sell_price, pair_idx
                )
                
                print(f"   [PAIR RESET] Pair {pair_idx} fully reset. Sentries re-armed.")
                self.save_state()
            
            # Update last check time
            self.last_deal_check_time = time.time()
            
        except Exception as e:
            print(f"[ERROR] _check_tp_sl_from_history failed: {e}")
            # Don't crash, just skip this tick

    async def _check_and_reopen(self):
        """
        Monitor for ANY closed position in a pair. 
        If detected, Trigger 'Pair Nuclear Reset': Close ALL other positions for that pair immediately.
        """
        # 1. Snapshot Init
        if not hasattr(self, "_pos_snapshot"):
            self._pos_snapshot = {}
            current_positions = mt5.positions_get(symbol=self.symbol)
            if current_positions:
                for p in current_positions:
                    if p.magic >= 50000:
                        self._pos_snapshot[p.ticket] = {"magic": p.magic, "pair_index": p.magic - 50000}
            return

        # 2. Get Current State
        current_positions = mt5.positions_get(symbol=self.symbol)
        
        # [FIX] Safety check: If positions_get returns None (error/disconnect), 
        # DO NOT assume positions are closed. Abort check this tick.
        if current_positions is None:
            # Optional: Print warning only if it persists, to avoid log spam
            # print(f" {self.symbol}: Connection issue? positions_get returned None.")
            return

        current_map = {}
        if current_positions:
            for p in current_positions:
                if p.magic >= 50000:
                    current_map[p.ticket] = {"magic": p.magic, "pair_index": p.magic - 50000}

        # 3. Detect Closed Tickets (In snapshot but not in current)
        closed_tickets = set(self._pos_snapshot.keys()) - set(current_map.keys())
        
        # [FIX] Debounce: Don't trust a single 'missing' signal.
        # Initialize a counter map if it doesn't exist
        if not hasattr(self, "_missing_pos_counters"):
            self._missing_pos_counters = defaultdict(int)

        # Update counters
        confirmed_closed_tickets = []
        
        # Check newly missing tickets
        for ticket in closed_tickets:
            self._missing_pos_counters[ticket] += 1
            # Require 3 consecutive confirmations (approx 0.5-1.5s) to believe it's closed
            # Require 3 consecutive confirmations (approx 0.5-1.5s) to believe it's closed
            if self._missing_pos_counters[ticket] >= 3:
                confirmed_closed_tickets.append(ticket)
        
        # Reset counters for tickets that reappeared (false alarm)
        for ticket in list(self._missing_pos_counters.keys()):
            if ticket not in closed_tickets:
                del self._missing_pos_counters[ticket]
        
        # 4. Handle Confirmed Closed Tickets
        for ticket in confirmed_closed_tickets:
            # Get info about the closed ticket from snapshot
            info = self._pos_snapshot.get(ticket)
            if not info:
                continue
                
            pair_idx = info['pair_index']
            pair = self.pairs.get(pair_idx)
            
            if pair:
                print(f" {self.symbol}: Detected TP/SL Close for Position {ticket} (Pair {pair_idx})")
                
                # CRITICAL FIX: Reset trade count to 0 so next trade starts at Lot 0
                print(f"   [RESET] Pair {pair_idx} trade_count reset to 0 (was {pair.trade_count})")
                pair.trade_count = 0
                
                # Close the other side if open (Nuclear Reset for this pair)
                # But FIRST, mark the closed side as NOT filled
                if pair.buy_ticket == ticket:
                    pair.buy_filled = False
                    pair.buy_ticket = 0
                    
                    # If sell is still open, close it
                    if pair.sell_filled and pair.sell_ticket:
                        print(f"   [PAIR RESET] Closing opposite Sell {pair.sell_ticket}...")
                        self._close_position(pair.sell_ticket)
                        pair.sell_filled = False
                        pair.sell_ticket = 0
                
                elif pair.sell_ticket == ticket:
                    pair.sell_filled = False
                    pair.sell_ticket = 0
                    
                    # If buy is still open, close it
                    if pair.buy_filled and pair.buy_ticket:
                        print(f"   [PAIR RESET] Closing opposite Buy {pair.buy_ticket}...")
                        self._close_position(pair.buy_ticket)
                        pair.buy_filled = False
                        pair.buy_ticket = 0
                
                # Reset flags
                pair.buy_in_zone = False
                pair.sell_in_zone = False
                pair.first_fill_direction = ""
                
                # Re-place triggers for this pair (both sides)
                # Since we reset trade_count to 0, next trade will be trade #1 (index 0)
                
                # Remove any existing pending orders for this pair
                if pair.buy_pending_ticket:
                    self._cancel_order(pair.buy_pending_ticket)
                if pair.sell_pending_ticket:
                    self._cancel_order(pair.sell_pending_ticket)
                
                # Place new triggers
                pair.buy_pending_ticket = self._place_pending_order(
                    self._get_order_type("buy", pair.buy_price),
                    pair.buy_price, pair_idx
                )
                pair.sell_pending_ticket = self._place_pending_order(
                    self._get_order_type("sell", pair.sell_price),
                    pair.sell_price, pair_idx
                )
                
                print(f"   [PAIR RESET] Pair {pair_idx} fully reset. Sentries re-armed.")
                self.save_state()
            
            # Remove from snapshot and counters
            del self._pos_snapshot[ticket]
            if ticket in self._missing_pos_counters:
                del self._missing_pos_counters[ticket]

            if ticket in current_map:
                del self._missing_pos_counters[ticket]

        for ticket in confirmed_closed_tickets:
            # Clean up counter
            del self._missing_pos_counters[ticket]
            
            info = self._pos_snapshot[ticket]
            pair_idx = info["pair_index"]
            pair = self.pairs.get(pair_idx)
            
            if not pair: continue

            # Determine if TP was hit
            tp_hit = self._check_if_tp_hit(ticket, "") # Direction not needed for profit check
            
            print(f" {self.symbol}: Position closed for Pair {pair_idx}. NUCLEAR RESET for this pair.")
            
            # --- PAIR NUCLEAR RESET ---
            # Force close EVERYTHING for this pair immediately
            self._close_pair_positions(pair_idx, "buy")
            self._close_pair_positions(pair_idx, "sell")

            # Reset Pair State
            pair.buy_filled = False
            # ... rest of reset logic ...
            pair.buy_ticket = 0
            pair.buy_in_zone = False
            pair.sell_filled = False 
            pair.sell_ticket = 0
            pair.sell_in_zone = False
            pair.pair_tp = 0.0
            pair.pair_sl = 0.0

            # Log
            self._log_trade(
                event_type="TP_HIT" if tp_hit else "SL_HIT",
                pair_index=pair_idx,
                direction="BOTH",
                price=0, lot_size=0, ticket=ticket,
                notes="Pair Nuclear Reset Triggered"
            )

            # Leapfrog or Reopen
            if tp_hit:
                # If it was a profitable close, try to leapfrog
                # We infer direction from the grid: if index > 0 (positive), trend is UP. 
                # Simplification: Try both or check which bound we are near.
                # Safer: Just leapfrog based on index polarity or price location
                tick = mt5.symbol_info_tick(self.symbol)
                if tick:
                    if tick.bid > pair.buy_price: # Price is high -> Leapfrog Up
                        untriggered = self._find_furthest_untriggered("up")
                        if untriggered is not None: await self._do_leapfrog_untriggered_up(untriggered)
                    elif tick.ask < pair.sell_price: # Price is low -> Leapfrog Down
                        untriggered = self._find_furthest_untriggered("down")
                        if untriggered is not None: await self._do_leapfrog_untriggered_down(untriggered)


            # Reset counters BEFORE re-arming to ensure fresh start (Lot 1)
            pair.trade_count = 0
            pair.first_fill_direction = ""
            
            # [FIX] Strict Reset: Always follow grid polarity.
            # Negative Pairs -> Buy first.
            # Positive Pairs -> Sell first.
            if pair_idx > 0:
                pair.next_action = "sell"
            else:
                pair.next_action = "buy"

            # Always re-arm at current levels
            self._reopen_pair_at_same_level(pair_idx)
            self.save_state()

        # 4. Update Snapshot
        self._pos_snapshot = current_map

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
    
    def _close_pair_positions(self, pair_index: int, direction_to_close: str):
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
                        time.sleep(0.1)
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
                        time.sleep(0.2) # Short pause to let quotes refresh
                    
                    else:
                        print(f"[CLOSE] {self.symbol}: Order send failed. Retrying...")
                        time.sleep(0.2)
    
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
    
    def _reopen_pair_at_same_level(self, pair_index: int):
        """Re-arm triggers at the same price level after TP/SL hit (no leapfrog)."""
        pair = self.pairs.get(pair_index)
        if not pair:
            return
        
        # [FIX] Reset trade_count to 0 so lot sizes restart from first value
        pair.trade_count = 0
        
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
        print(f"[REOPEN] {self.symbol}: Pair {pair_index} re-armed at same levels (B@{pair.buy_price:.2f}, S@{pair.sell_price:.2f}) - lot reset to first")
    
    def _create_next_positive_pair(self, edge_idx: int):
        """
        Create the next positive pair beyond the current edge.
        Called when edge positive pair triggers - expands grid upward.
        
        New pair structure: S[n+1] = B[n], B[n+1] = S[n+1] + spread
        """
        edge_pair = self.pairs.get(edge_idx)
        if not edge_pair:
            return
        
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
        ticket = self._execute_market_order("sell", new_sell_price, new_idx)
        
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
            if not edge_pair.buy_filled:
                if abs(edge_pair.buy_price - new_sell_price) < 1.0:
                    print(f" {self.symbol}: CHAIN B{edge_idx} @ {edge_pair.buy_price:.2f} (from expansion)")
                    chain_ticket = self._execute_market_order("buy", edge_pair.buy_price, edge_idx)
                    if chain_ticket:
                        edge_pair.buy_filled = True
                        edge_pair.buy_ticket = chain_ticket
                        edge_pair.buy_pending_ticket = 0
                        edge_pair.advance_toggle()
            
            print(f" {self.symbol}: Pair {new_idx} Active. S filled (0.01), B pending (0.02) @ {new_buy_price:.2f}")
        else:
            # Fallback
            new_pair.sell_pending_ticket = self._place_pending_order("sell_limit", new_sell_price, new_idx)
            new_pair.buy_pending_ticket = self._place_pending_order("buy_stop", new_buy_price, new_idx)
        
        self.save_state()

    def _create_next_negative_pair(self, edge_idx: int):
        """
        Create the next negative pair beyond the current edge.
        Called when edge negative pair triggers - expands grid downward.
        
        New pair structure: B[n-1] = S[n], S[n-1] = B[n-1] - spread
        """
        edge_pair = self.pairs.get(edge_idx)
        if not edge_pair:
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
        ticket = self._execute_market_order("buy", new_buy_price, new_idx)
        
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
            if not edge_pair.sell_filled:
                if abs(edge_pair.sell_price - new_buy_price) < 1.0:
                    print(f" {self.symbol}: CHAIN S{edge_idx} @ {edge_pair.sell_price:.2f} (from expansion)")
                    chain_ticket = self._execute_market_order("sell", edge_pair.sell_price, edge_idx)
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
        
        self.save_state()
    async def _do_leapfrog_untriggered_up(self, untriggered_idx: int):
        """
        Leapfrog a specific UNTRIGGERED pair to the top (price trending UP).
        
        - Remove the untriggered pair
        - Create new pair at top with SELL immediately at market
        - Set BUY trigger at spread above
        """
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
        volume = new_pair.get_next_lot(self.lot_sizes)
        ticket = self._execute_market_order("sell", exec_sell_price, new_idx)
        
        if ticket:
            new_pair.sell_filled = True
            new_pair.sell_ticket = ticket
            new_pair.sell_in_zone = True
            new_pair.advance_toggle()  # Now next_action = "buy"
            
            # Arm BUY trigger
            new_pair.buy_pending_ticket = self._place_pending_order("buy_stop", new_buy_price, new_idx)
            
            print(f" {self.symbol}: LEAPFROG UNTRIGGERED UP | Pair {untriggered_idx} → {new_idx} | SELL@{exec_sell_price:.2f}")
        else:
            # Failed - just arm triggers
            new_pair.sell_pending_ticket = self._place_pending_order("sell_limit", exec_sell_price, new_idx)
            new_pair.buy_pending_ticket = self._place_pending_order("buy_stop", new_buy_price, new_idx)
        
        self.print_grid_table()
        self.save_state()
    
    async def _do_leapfrog_untriggered_down(self, untriggered_idx: int):
        """
        Leapfrog a specific UNTRIGGERED pair to the bottom (price trending DOWN).
        
        - Remove the untriggered pair
        - Create new pair at bottom with BUY immediately at market
        - Set SELL trigger at spread below
        """
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
        volume = new_pair.get_next_lot(self.lot_sizes)
        ticket = self._execute_market_order("buy", exec_buy_price, new_idx)
        
        if ticket:
            new_pair.buy_filled = True
            new_pair.buy_ticket = ticket
            new_pair.buy_in_zone = True
            new_pair.advance_toggle()  # Now next_action = "sell"
            
            # Arm SELL trigger
            new_pair.sell_pending_ticket = self._place_pending_order("sell_stop", new_sell_price, new_idx)
            
            print(f" {self.symbol}: LEAPFROG UNTRIGGERED DOWN | Pair {untriggered_idx} → {new_idx} | BUY@{exec_buy_price:.2f}")
        else:
            # Failed - just arm triggers
            new_pair.buy_pending_ticket = self._place_pending_order("buy_limit", exec_buy_price, new_idx)
            new_pair.sell_pending_ticket = self._place_pending_order("sell_stop", new_sell_price, new_idx)
        
        self.print_grid_table()
        self.save_state()
    
    async def _do_leapfrog_up(self):
        """
        Leapfrog the bottom pair to the top (price trending UP).
        
        SMART EXECUTION:
        - Price is trending UP, so SELL immediately at current market (bid)
        - Set BUY trigger at SELL price + spread (above the sell)
        - This captures the upward movement with the sell, then prepares for reversal
        
        Toggle: After sell executes, next_action = "buy"
        """
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
        volume = new_pair.get_next_lot(self.lot_sizes)
        ticket = self._execute_market_order("sell", exec_sell_price, new_idx)
        
        if ticket:
            new_pair.sell_filled = True
            new_pair.sell_ticket = ticket
            new_pair.sell_in_zone = True
            new_pair.advance_toggle()  # Now next_action = "buy"
            
            # Arm BUY trigger at spread above
            new_pair.buy_pending_ticket = self._place_pending_order("buy_stop", new_buy_price, new_idx)
            
            # Log leapfrog event
            self._log_trade(
                event_type="LEAPFROG_UP",
                pair_index=new_idx,
                direction="SELL",
                price=exec_sell_price,
                lot_size=volume,
                ticket=ticket,
                notes=f"Pair {min_idx} → {new_idx} | SELL@MKT, B@{new_buy_price:.2f}"
            )
        else:
            # Failed to execute, just set up triggers
            new_pair.next_action = "sell"
            new_pair.sell_pending_ticket = self._place_pending_order("sell_limit", exec_sell_price, new_idx)
            new_pair.buy_pending_ticket = self._place_pending_order("buy_stop", new_buy_price, new_idx)
            print(f" {self.symbol}: LEAPFROG UP sell failed, armed triggers instead")
        
        # Print grid table after leapfrog for visualization
        self.print_grid_table()
        self.save_state()
    
    async def _do_leapfrog_down(self):
        """
        Leapfrog the top pair to the bottom (price trending DOWN).
        
        SMART EXECUTION:
        - Price is trending DOWN, so BUY immediately at current market (ask)
        - Set SELL trigger at BUY price - spread (below the buy)
        - This catches the bottom with the buy, then prepares for reversal
        
        Toggle: After buy executes, next_action = "sell"
        """
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
        volume = new_pair.get_next_lot(self.lot_sizes)
        ticket = self._execute_market_order("buy", exec_buy_price, new_idx)
        
        if ticket:
            new_pair.buy_filled = True
            new_pair.buy_ticket = ticket
            new_pair.buy_in_zone = True
            new_pair.advance_toggle()  # Now next_action = "sell"
            
            # Arm SELL trigger at spread below
            new_pair.sell_pending_ticket = self._place_pending_order("sell_stop", new_sell_price, new_idx)
            
            # Log leapfrog event
            self._log_trade(
                event_type="LEAPFROG_DOWN",
                pair_index=new_idx,
                direction="BUY",
                price=exec_buy_price,
                lot_size=volume,
                ticket=ticket,
                notes=f"Pair {max_idx} → {new_idx} | BUY@MKT, S@{new_sell_price:.2f}"
            )
        else:
            # Failed to execute, just set up triggers
            new_pair.next_action = "buy"
            new_pair.buy_pending_ticket = self._place_pending_order("buy_limit", exec_buy_price, new_idx)
            new_pair.sell_pending_ticket = self._place_pending_order("sell_stop", new_sell_price, new_idx)
            print(f" {self.symbol}: LEAPFROG DOWN buy failed, armed triggers instead")
        
        # Print grid table after leapfrog for visualization
        self.print_grid_table()
        self.save_state()
    
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
            self.save_state()
        
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
            self.save_state()
    
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
    
    def _get_lot_size(self, index: int, direction: str) -> float:
        """
        Get lot size using per-pair trade counter.
        Formula: T[i] = L[i] (Trade #N uses Lot Size #N)
        """
        pair = self.pairs.get(index)
        if not pair:
            # Pair not found, use first lot
            return self.lot_sizes[0] if self.lot_sizes else 0.01
        
        # Use the pair's trade counter to get the correct lot
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
    
    def _check_virtual_triggers(self, ask: float, bid: float):
        """
        Check triggers and fire market orders.
        FIXED: 
        1. Prevents 'Latching' on failed trades (retries immediately).
        2. ALWAYS allows the 'Hedge' trade (2nd leg) to open even if Max Positions is reached,
           preventing 'Naked' positions.
        """
        sorted_items = sorted(self.pairs.items(), key=lambda x: x[0])
        
        for idx, pair in sorted_items:
            # VALIDATION
            expected_buy_price = pair.sell_price + self.spread
            if abs(pair.buy_price - expected_buy_price) > 0.5:
                pair.buy_price = pair.sell_price + self.spread
                self.save_state()
            
            # --- BUY TRIGGER ---
            if idx > 0:   buy_in_zone_now = ask >= pair.buy_price
            elif idx < 0: buy_in_zone_now = bid <= pair.buy_price
            else:         buy_in_zone_now = ask >= pair.buy_price
            
            # Zone EXIT (Re-arm virtual trigger)
            if pair.buy_in_zone and not buy_in_zone_now:
                pair.buy_in_zone = False
                if pair.buy_pending_ticket == 0:
                    pair.buy_pending_ticket = self._place_pending_order(
                        self._get_order_type("buy", pair.buy_price), pair.buy_price, idx
                    )

            # Zone ENTRY
            buy_attempt_failed = False
            if buy_in_zone_now and not pair.buy_in_zone and pair.next_action == "buy":
                # LOGIC FIX: Allow trade if under limit OR if completing a pair (odd count)
                # trade_count is 0-based. 
                # 0 (New) -> check limit. 
                # 1 (Hedge) -> ALWAYS ALLOW. 
                # 2 (New) -> check limit.
                is_hedge_trade = (pair.trade_count % 2 != 0)
                
                if is_hedge_trade or (pair.trade_count < self.max_positions):
                    print(f" {self.symbol}: BUY @ Pair {idx} ({pair.buy_price:.2f})")
                    ticket = self._execute_market_order("buy", pair.buy_price, idx)
                    if ticket:
                        pair.buy_filled = True
                        pair.buy_ticket = ticket
                        pair.buy_pending_ticket = 0
                        pair.advance_toggle()
                        
                        # [CRITICAL FIX] FORWARD: Check if next pair exists, if not CREATE it first
                        # Grid structure: S[N] = B[N-1], so when B[N] triggers, S[N+1] should also trigger
                        next_idx = idx + 1
                        next_pair = self.pairs.get(next_idx)
                        
                        # If next pair doesn't exist, we need to expand the grid upward
                        if not next_pair:
                            print(f" {self.symbol}: B{idx} triggered, but S{next_idx} doesn't exist. Creating Pair {next_idx}...")
                            # Create new pair above
                            new_sell_price = pair.buy_price  # S[next_idx] = B[idx]
                            new_buy_price = new_sell_price + self.spread
                            
                            new_pair = GridPair(
                                index=next_idx,
                                buy_price=new_buy_price,
                                sell_price=new_sell_price
                            )
                            new_pair.next_action = "sell"
                            self.pairs[next_idx] = new_pair
                            
                            # Execute SELL immediately at market
                            print(f" {self.symbol}: Executing S{next_idx} @ {new_sell_price:.2f} (expansion + chain)")
                            chain_ticket = self._execute_market_order("sell", new_sell_price, next_idx)
                            if chain_ticket:
                                new_pair.sell_filled = True
                                new_pair.sell_ticket = chain_ticket
                                new_pair.sell_in_zone = True
                                new_pair.advance_toggle()
                                
                                # Arm BUY trigger for this new pair
                                new_pair.buy_pending_ticket = self._place_pending_order("buy_limit", new_buy_price, next_idx)
                                print(f" {self.symbol}: Pair {next_idx} created with S filled, B pending @ {new_buy_price:.2f}")
                            
                            next_pair = new_pair  # Update reference
                        
                        # Now execute normal forward chain (if pair exists and SELL not filled)
                        if next_pair and not next_pair.sell_filled:
                            # [FIX] Safety Check 1: Verify position doesn't already exist in MT5
                            existing_pos = mt5.positions_get(ticket=next_pair.sell_ticket) if next_pair.sell_ticket else None
                            if existing_pos:
                                print(f" {self.symbol}: CHAIN SKIP S{next_idx} - already exists (ticket {next_pair.sell_ticket})")
                            else:
                                # Check price match with increased tolerance
                                price_diff = abs(next_pair.sell_price - pair.buy_price)
                                if price_diff < 11.0:
                                    print(f" {self.symbol}: CHAIN S{next_idx} @ {next_pair.sell_price:.2f} (diff: {price_diff:.2f})")
                                    chain_ticket = self._execute_market_order("sell", next_pair.sell_price, next_idx)
                                    if chain_ticket:
                                        next_pair.sell_filled = True
                                        next_pair.sell_ticket = chain_ticket
                                        next_pair.sell_pending_ticket = 0
                                        next_pair.advance_toggle()
                                else:
                                    # Beyond tolerance but entry was missed - execute at market anyway
                                    print(f" {self.symbol}: CHAIN S{next_idx} @ MARKET (diff {price_diff:.2f} > 11.0)")
                                    chain_ticket = self._execute_market_order("sell", next_pair.sell_price, next_idx)
                                    if chain_ticket:
                                        next_pair.sell_filled = True
                                        next_pair.sell_ticket = chain_ticket
                                        next_pair.sell_pending_ticket = 0
                                        next_pair.advance_toggle()
                        
                        # Expand Grid Up (legacy check - should mostly be handled above now)
                        indices = sorted(self.pairs.keys())
                        if idx == indices[-1] and idx >= 0:
                            self._create_next_positive_pair(idx)
                        self.save_state()
                    else:
                        print(f" {self.symbol}: Buy failed for Pair {idx}, retrying next tick...")
                        buy_attempt_failed = True
                else:
                    # Only latch if we are strictly blocked by logic, not failure
                    pair.buy_in_zone = True 

            if not buy_attempt_failed:
                pair.buy_in_zone = buy_in_zone_now
            
            # --- SELL TRIGGER ---
            if idx > 0:   sell_in_zone_now = ask >= pair.sell_price
            elif idx < 0: sell_in_zone_now = bid <= pair.sell_price
            else:         sell_in_zone_now = bid <= pair.sell_price
            
            # Zone EXIT
            if pair.sell_in_zone and not sell_in_zone_now:
                pair.sell_in_zone = False
                if pair.sell_pending_ticket == 0:
                    pair.sell_pending_ticket = self._place_pending_order(
                        self._get_order_type("sell", pair.sell_price), pair.sell_price, idx
                    )

            # Zone ENTRY
            sell_attempt_failed = False
            if sell_in_zone_now and not pair.sell_in_zone and pair.next_action == "sell":
                # LOGIC FIX: Allow trade if under limit OR if completing a pair (odd count)
                is_hedge_trade = (pair.trade_count % 2 != 0)
                
                if is_hedge_trade or (pair.trade_count < self.max_positions):
                    print(f" {self.symbol}: SELL @ Pair {idx} ({pair.sell_price:.2f})")
                    ticket = self._execute_market_order("sell", pair.sell_price, idx)
                    if ticket:
                        pair.sell_filled = True
                        pair.sell_ticket = ticket
                        pair.sell_pending_ticket = 0
                        pair.advance_toggle()
                        
                        #For downwards expansion 

                        # [CRITICAL FIX] BACKWARD: Check if previous pair exists, if not CREATE it first
                        # Grid structure: S[N] = B[N-1], so when S[N] triggers, B[N-1] should also trigger
                        prev_idx = idx - 1
                        prev_pair = self.pairs.get(prev_idx)
                        
                        # If previous pair doesn't exist, we need to expand the grid downward
                        if not prev_pair:
                            print(f" {self.symbol}: S{idx} triggered, but B{prev_idx} doesn't exist. Creating Pair {prev_idx}...")
                            # Create new pair below
                            new_buy_price = pair.sell_price  # B[prev_idx] = S[idx]
                            new_sell_price = new_buy_price - self.spread
                            
                            new_pair = GridPair(
                                index=prev_idx,
                                buy_price=new_buy_price,
                                sell_price=new_sell_price
                            )
                            new_pair.next_action = "buy"
                            self.pairs[prev_idx] = new_pair
                            
                            # Execute BUY immediately at market
                            print(f" {self.symbol}: Executing B{prev_idx} @ {new_buy_price:.2f} (expansion + chain)")
                            chain_ticket = self._execute_market_order("buy", new_buy_price, prev_idx)
                            if chain_ticket:
                                new_pair.buy_filled = True
                                new_pair.buy_ticket = chain_ticket
                                new_pair.buy_in_zone = True
                                new_pair.advance_toggle()
                                
                                # Arm SELL trigger for this new pair
                                new_pair.sell_pending_ticket = self._place_pending_order("sell_stop", new_sell_price, prev_idx)
                                print(f" {self.symbol}: Pair {prev_idx} created with B filled, S pending @ {new_sell_price:.2f}")
                            
                            prev_pair = new_pair  # Update reference
                        
                        # Now execute normal backward chain (if pair exists and BUY not filled)
                        if prev_pair and not prev_pair.buy_filled:
                            # [FIX] Safety Check 1: Verify position doesn't already exist in MT5
                            existing_pos = mt5.positions_get(ticket=prev_pair.buy_ticket) if prev_pair.buy_ticket else None
                            if existing_pos:
                                print(f" {self.symbol}: CHAIN SKIP B{prev_idx} - already exists (ticket {prev_pair.buy_ticket})")
                            else:
                                # Check price match with increased tolerance
                                price_diff = abs(prev_pair.buy_price - pair.sell_price)
                                if price_diff < 11.0:
                                    print(f" {self.symbol}: CHAIN B{prev_idx} @ {prev_pair.buy_price:.2f} (diff: {price_diff:.2f})")
                                    chain_ticket = self._execute_market_order("buy", prev_pair.buy_price, prev_idx)
                                    if chain_ticket:
                                        prev_pair.buy_filled = True
                                        prev_pair.buy_ticket = chain_ticket
                                        prev_pair.buy_pending_ticket = 0
                                        prev_pair.advance_toggle()
                                else:
                                    # Beyond tolerance but entry was missed - execute at market anyway
                                    print(f" {self.symbol}: CHAIN B{prev_idx} @ MARKET (diff {price_diff:.2f} > 11.0)")
                                    chain_ticket = self._execute_market_order("buy", prev_pair.buy_price, prev_idx)
                                    if chain_ticket:
                                        prev_pair.buy_filled = True
                                        prev_pair.buy_ticket = chain_ticket
                                        prev_pair.buy_pending_ticket = 0
                                        prev_pair.advance_toggle()
                        
                        # Expand Grid Down (legacy check - should mostly be handled above now)
                        indices = sorted(self.pairs.keys())
                        if idx == indices[0] and idx <= 0:
                            self._create_next_negative_pair(idx)
                        self.save_state()
                    else:
                        print(f" {self.symbol}: Sell failed for Pair {idx}, retrying next tick...")
                        sell_attempt_failed = True
                else:
                    pair.sell_in_zone = True

            if not sell_attempt_failed:
                pair.sell_in_zone = sell_in_zone_now
        
        # [FIX] Retroactive Catch-Up Scan: Check for missed chains
        # Safety Check 2: Limit scope to recently touched pairs (avoid scanning entire grid every tick)
        # We'll check the last triggered pair ± 2 levels
        if sorted_items:
            last_idx = sorted_items[-1][0]  # Last processed pair index
            tick = mt5.symbol_info_tick(self.symbol)
            
            for offset in range(-2, 3):  # -2, -1, 0, +1, +2
                check_idx = last_idx + offset
                check_pair = self.pairs.get(check_idx)
                
                if not check_pair:
                    continue
                
                # Check if BUY side should have chained but didn't
                if not check_pair.buy_filled:
                    prev_idx = check_idx - 1
                    prev_pair = self.pairs.get(prev_idx)
                    if prev_pair and prev_pair.sell_filled:
                        # S[prev] filled, but B[check] NOT filled
                        # They should be at same price - check if this was a missed chain
                        price_diff = abs(check_pair.buy_price - prev_pair.sell_price)
                        if price_diff < 7.0:
                            # Safety Check 3: Price freshness - only execute if current price is within tolerance
                            current_price_diff = abs(tick.ask - check_pair.buy_price)
                            if current_price_diff < 7.0:
                                print(f" {self.symbol}: LATE CHAIN B{check_idx} @ {check_pair.buy_price:.2f} (missed earlier)")
                                chain_ticket = self._execute_market_order("buy", check_pair.buy_price, check_idx)
                                if chain_ticket:
                                    check_pair.buy_filled = True
                                    check_pair.buy_ticket = chain_ticket
                                    check_pair.buy_pending_ticket = 0
                                    check_pair.advance_toggle()
                                    self.save_state()
                
                # Check if SELL side should have chained but didn't
                if not check_pair.sell_filled:
                    next_idx = check_idx + 1
                    next_pair = self.pairs.get(next_idx)
                    if next_pair and next_pair.buy_filled:
                        # B[next] filled, but S[check] NOT filled
                        # They should be at same price - check if this was a missed chain
                        price_diff = abs(check_pair.sell_price - next_pair.buy_price)
                        if price_diff < 7.0:
                            # Safety Check 3: Price freshness - only execute if current price is within tolerance
                            current_price_diff = abs(tick.bid - check_pair.sell_price)
                            if current_price_diff < 7.0:
                                print(f" {self.symbol}: LATE CHAIN S{check_idx} @ {check_pair.sell_price:.2f} (missed earlier)")
                                chain_ticket = self._execute_market_order("sell", check_pair.sell_price, check_idx)
                                if chain_ticket:
                                    check_pair.sell_filled = True
                                    check_pair.sell_ticket = chain_ticket
                                    check_pair.sell_pending_ticket = 0
                                    check_pair.advance_toggle()
                                    self.save_state()

    def _execute_market_order(self, direction: str, price: float, index: int) -> int:
        """Execute a market order and return the position ticket.
        
        TP/SL ALIGNMENT LOGIC:
        - First trade in a pair: Use UI input to set TP and SL normally
        - Second trade in same pair: Buy TP = Sell SL, Buy SL = Sell TP
        - This ensures both positions share the same exit levels
        """
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return 0
        
        exec_price = tick.ask if direction == "buy" else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
        
        # Get volume based on pair section and fill order
        volume = self._get_lot_size(index, direction)
        
        # Get the pair to check if TP/SL levels are already set
        pair = self.pairs.get(index)
        
        # Always calculate default TP/SL from UI first
        tp_pips = float(self.config.get(f'{direction}_stop_tp', 20.0))
        sl_pips = float(self.config.get(f'{direction}_stop_sl', 20.0))
        
        if direction == "buy":
            tp = exec_price + tp_pips
            sl = exec_price - sl_pips
        else:
            tp = exec_price - tp_pips
            sl = exec_price + sl_pips
        
        # TP/SL ALIGNMENT: On second trade of a cycle, use aligned levels
        # trade_count: 0 (1st), 1 (2nd), 2 (3rd=new 1st), 3 (4th=new 2nd), ...
        # ODD trade_count = second trade of cycle
        if pair and (pair.trade_count % 2 == 1) and pair.pair_tp != 0.0 and pair.pair_sl != 0.0:
            # SECOND TRADE: Use aligned TP/SL (Buy TP = Sell SL, Buy SL = Sell TP)
            if direction == "buy":
                tp = pair.pair_sl  # Buy TP = where Sell's SL was (above)
                sl = pair.pair_tp  # Buy SL = where Sell's TP was (below)
            else:
                tp = pair.pair_sl  # Sell TP = where Buy's SL was (below)
                sl = pair.pair_tp  # Sell SL = where Buy's TP was (above)
        else:
            # FIRST TRADE: Store these levels for the second trade
            if pair:
                pair.pair_tp = tp
                pair.pair_sl = sl
        
        magic = 50000 + index
        
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
            "comment": f"L{index}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
            "deviation": 50
        }
        
        result = mt5.order_send(request)
        
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            # Log trade to history
            self._log_trade(
                event_type="OPEN",
                pair_index=index,
                direction=direction.upper(),
                price=exec_price,
                lot_size=volume,
                ticket=result.order,
                notes=f"TP={tp:.2f} SL={sl:.2f}"
            )
            return result.order
        elif result and result.retcode == mt5.TRADE_RETCODE_INVALID_STOPS:
            # Invalid stops - get minimum distance and retry
            symbol_info = mt5.symbol_info(self.symbol)
            if symbol_info:
                point = symbol_info.point
                min_dist = max(symbol_info.trade_stops_level * point, 10 * point)
                
                # Adjust TP/SL to minimum distance
                if direction == "buy":
                    tp = exec_price + max(tp_pips, min_dist)
                    sl = exec_price - max(sl_pips, min_dist)
                else:
                    tp = exec_price - max(tp_pips, min_dist)
                    sl = exec_price + max(sl_pips, min_dist)
                
                request["sl"] = float(sl)
                request["tp"] = float(tp)
                
                # Retry with adjusted stops
                result = mt5.order_send(request)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    # Log trade to history (adjusted stops)
                    self._log_trade(
                        event_type="OPEN",
                        pair_index=index,
                        direction=direction.upper(),
                        price=exec_price,
                        lot_size=volume,
                        ticket=result.order,
                        notes=f"TP={tp:.2f} SL={sl:.2f} (adj)"
                    )
                    return result.order
        
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
    
    def _close_position(self, position):
        """Close a specific position."""
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
            "deviation": 20,
        }
        mt5.order_send(request)
    
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
    
    def _log_trade(self, event_type: str, pair_index: int, direction: str, 
                   price: float, lot_size: float, ticket: int = 0, notes: str = ""):
        """
        Log a trade event to the history and print to console.
        Also writes to debug log file for persistence.
        """
        self.global_trade_counter += 1
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        
        pair = self.pairs.get(pair_index)
        trade_num = pair.trade_count if pair else 0
        
        log_entry = TradeLog(
            timestamp=timestamp,
            event_type=event_type,
            pair_index=pair_index,
            direction=direction,
            price=price,
            lot_size=lot_size,
            trade_num=trade_num,
            ticket=ticket,
            notes=notes
        )
        
        self.trade_history.append(log_entry)
        
        # Print to console with color-coded output
        icon = {
            "OPEN": "",
            "TP_HIT": "",
            "SL_HIT": "",
            "LEAPFROG_UP": "",
            "LEAPFROG_DOWN": "",
            "REOPEN": ""
        }.get(event_type, "")
        
        print(f"{icon} #{self.global_trade_counter:03d} {log_entry}")
        
        # [FIX] Log to Session Logger for UI visibility
        if self.session_logger:
            self.session_logger.log_trade(
                symbol=self.symbol,
                pair_idx=pair_index,
                direction=direction,
                price=price,
                lot=lot_size,
                trade_num=trade_num,
                ticket=ticket
            )
        
        # Append to debug file
        try:
            with open(self.debug_log_file, "a") as f:
                f.write(f"#{self.global_trade_counter:03d} {log_entry}\n")
        except Exception:
            pass
    
    def print_grid_table(self):
        """
        Print a visual ASCII table of the current grid state.
        Shows: Pair Index | Buy Price | Sell Price | Next Action | Trade Count | Status
        """
        if not self.pairs:
            print(f"\n {self.symbol}: Grid is empty\n")
            return
        
        # Header
        header = f"""
╔═══════════════════════════════════════════════════════════════════════════════════════╗
║  {self.symbol:^20}  │  Phase: {self.phase:<15}  │  Price: {self.current_price:>10.2f}  │  Cycle: {self.iteration}  ║
╠══════╦═══════════════╦═══════════════╦════════════╦═══════════╦════════════════════════╣
║ Pair ║   BUY Price   ║  SELL Price   ║ Next Action║ Trade Cnt ║        Status          ║
╠══════╬═══════════════╬═══════════════╬════════════╬═══════════╬════════════════════════╣"""
        print(header)
        
        # Sort by index (highest to lowest for visual clarity)
        sorted_indices = sorted(self.pairs.keys(), reverse=True)
        
        for idx in sorted_indices:
            pair = self.pairs[idx]
            
            # Status flags
            buy_status = " FILLED" if pair.buy_filled else (" ARMED" if pair.buy_pending_ticket else " NONE")
            sell_status = " FILLED" if pair.sell_filled else (" ARMED" if pair.sell_pending_ticket else " NONE")
            combined_status = f"B:{buy_status[:6]} S:{sell_status[:6]}"
            
            # Highlight current price zone
            in_zone = ""
            if pair.sell_price <= self.current_price <= pair.buy_price:
                in_zone = " ZONE"
            
            row = f"║ {idx:>4} ║ {pair.buy_price:>13.2f} ║ {pair.sell_price:>13.2f} ║ {pair.next_action:^10} ║ {pair.trade_count:>9} ║ {combined_status:<22} ║"
            print(row)
        
        footer = "╚══════╩═══════════════╩═══════════════╩════════════╩═══════════╩════════════════════════╝"
        print(footer)
        
        # Lot sizes reference
        print(f"\n Lot Sizes: {self.lot_sizes}  │  Spread: {self.spread}  │  Max Pairs: {self.max_pairs}")
        print(f" Trade History: {len(self.trade_history)} events  │  Log File: {self.debug_log_file}\n")
    
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

    
    def save_state(self):
        """Persist grid state to disk."""
        pairs_data = {}
        for idx, pair in self.pairs.items():
            pairs_data[str(idx)] = {
                "index": pair.index,
                "buy_price": pair.buy_price,
                "sell_price": pair.sell_price,
                "buy_ticket": pair.buy_ticket,
                "sell_ticket": pair.sell_ticket,
                "buy_filled": pair.buy_filled,
                "sell_filled": pair.sell_filled,
                "buy_pending_ticket": pair.buy_pending_ticket,
                "sell_pending_ticket": pair.sell_pending_ticket,
            }
        
        state = {
            "phase": self.phase,
            "center_price": self.center_price,
            "pairs": pairs_data,
            "iteration": self.iteration,
        }
        
        try:
            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            print(f" {self.symbol}: Failed to save state: {e}")
    
    def load_state(self):
        """Load grid state from disk."""
        if not os.path.exists(self.state_file):
            return
        
        try:
            with open(self.state_file, "r") as f:
                state = json.load(f)
            
            self.phase = state.get("phase", self.PHASE_INIT)
            self.center_price = state.get("center_price", 0.0)
            self.iteration = state.get("iteration", 1)
            
            pairs_data = state.get("pairs", {})
            self.pairs = {}
            for idx_str, pair_data in pairs_data.items():
                idx = int(idx_str)
                self.pairs[idx] = GridPair(
                    index=pair_data["index"],
                    buy_price=pair_data["buy_price"],
                    sell_price=pair_data["sell_price"],
                    buy_ticket=pair_data.get("buy_ticket", 0),
                    sell_ticket=pair_data.get("sell_ticket", 0),
                    buy_filled=pair_data.get("buy_filled", False),
                    sell_filled=pair_data.get("sell_filled", False),
                    buy_pending_ticket=pair_data.get("buy_pending_ticket", 0),
                    sell_pending_ticket=pair_data.get("sell_pending_ticket", 0),
                )
            
            print(f" {self.symbol}: Loaded state. Phase: {self.phase}, Pairs: {len(self.pairs)}")
            
        except Exception as e:
            print(f" {self.symbol}: Failed to load state: {e}")


# Alias for backward compatibility
GridStrategy = LadderGridStrategy