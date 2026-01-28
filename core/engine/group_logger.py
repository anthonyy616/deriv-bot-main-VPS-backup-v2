"""
Group Logger - Structured logging for trading groups with table formatting.

Provides per-group tracking and visualization of:
- Pairs with trade type, entry, TP, SL, re-entries, lot sizes
- Group status (C count, anchor, direction, settled)
- Chronological event log
"""

import os
import time
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class PairLegData:
    """Data for a single leg (Buy or Sell) of a pair."""
    status: str = "PENDING" # PENDING, ACTIVE, TP, SL, CLOSED, WAITING
    entry: float = 0.0
    tp: float = 0.0
    sl: float = 0.0
    lots: float = 0.0
    ticket: int = 0
    re_entries: int = 0

@dataclass
class PairData:
    """Data for a single pair (index) containing both Buy and Sell legs."""
    pair_idx: int
    buy_leg: PairLegData = field(default_factory=PairLegData)
    sell_leg: PairLegData = field(default_factory=PairLegData)


@dataclass
class GroupData:
    """Data for a single trading group."""
    group_id: int
    init_direction: str = ""  # "BULLISH" or "BEARISH"
    pending_retracement: str = ""  # "BULLISH" or "BEARISH" - opposite of init
    anchor: float = 0.0
    c_count: int = 0
    settled: bool = False
    pairs: Dict[int, PairData] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)


class GroupLogger:
    """
    Structured logger for trading groups.
    Creates readable table-formatted logs per group.
    """
    # Table formatting constants
    HEADER_CHAR = "═"
    ROW_CHAR = "─"
    COL_SEP = "│"

    def __init__(self, symbol: str, log_dir: str = "logs", user_id: str = None):
        """Initialize the group logger."""
        self.symbol = symbol
        self.user_id = user_id
        self.groups: Dict[int, GroupData] = {}
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Determine log directory
        from pathlib import Path
        root_dir = Path(__file__).resolve().parent.parent.parent

        if user_id:
            self.log_dir = root_dir / "logs" / "users" / user_id / "sessions"
        else:
            self.log_dir = root_dir / log_dir

        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = str(self.log_dir)

        # Main session log file
        safe_symbol = symbol.replace(" ", "_").replace("/", "_")
        self.main_log_path = os.path.join(
            self.log_dir, f"groups_{safe_symbol}_{self.session_id}.log"
        )

    def _get_or_create_group(self, group_id: int) -> GroupData:
        """Get existing group or create new one."""
        if group_id not in self.groups:
            self.groups[group_id] = GroupData(group_id=group_id)
        return self.groups[group_id]
        
    def _get_or_create_pair(self, group: GroupData, pair_idx: int) -> PairData:
        if pair_idx not in group.pairs:
            group.pairs[pair_idx] = PairData(pair_idx=pair_idx)
        return group.pairs[pair_idx]

    def log_init(self, group_id: int, anchor: float, is_bullish_source: bool,
                 b_idx: int, s_idx: int, b_ticket: int = 0, s_ticket: int = 0,
                 b_entry: float = 0, s_entry: float = 0,
                 b_tp: float = 0, s_tp: float = 0,
                 b_sl: float = 0, s_sl: float = 0,
                 lots: float = 0.01):
        """Log group initialization."""
        group = self._get_or_create_group(group_id)
        group.anchor = anchor
        group.init_direction = "BULLISH" if is_bullish_source else "BEARISH"
        group.pending_retracement = "BEARISH" if is_bullish_source else "BULLISH"
        group.c_count = 0
        group.settled = False

        # Update Buy Index
        p_buy = self._get_or_create_pair(group, b_idx)
        p_buy.buy_leg.status = "ACTIVE" if b_ticket else "PENDING"
        p_buy.buy_leg.entry = b_entry if b_entry else anchor
        p_buy.buy_leg.tp = b_tp
        p_buy.buy_leg.sl = b_sl
        p_buy.buy_leg.lots = lots
        p_buy.buy_leg.ticket = b_ticket

        # Update Sell Index
        p_sell = self._get_or_create_pair(group, s_idx)
        p_sell.sell_leg.status = "ACTIVE" if s_ticket else "PENDING"
        p_sell.sell_leg.entry = s_entry if s_entry else anchor
        p_sell.sell_leg.tp = s_tp
        p_sell.sell_leg.sl = s_sl
        p_sell.sell_leg.lots = lots
        p_sell.sell_leg.ticket = s_ticket

        # Log event
        event = {
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "type": "INIT",
            "message": f"Group {group_id} INIT @ {anchor:.2f} ({group.init_direction} source)",
            "details": f"B{b_idx}+S{s_idx}, Pending retracement: {group.pending_retracement}"
        }
        group.events.append(event)
        self._write_event(group_id, event)

    def log_expansion(self, group_id: int, expansion_type: str,
                      pair_idx: int, trade_type: str, entry: float,
                      tp: float, sl: float, lots: float, ticket: int = 0,
                      seed_idx: int = None, seed_type: str = None,
                      seed_entry: float = None, seed_tp: float = None,
                      seed_sl: float = None, seed_ticket: int = 0,
                      is_atomic: bool = True, c_count: int = 0):
        """Log grid expansion."""
        group = self._get_or_create_group(group_id)
        group.c_count = c_count

        # Main completing pair
        p1 = self._get_or_create_pair(group, pair_idx)
        leg1 = p1.buy_leg if trade_type == "BUY" else p1.sell_leg
        leg1.status = "ACTIVE"
        leg1.entry = entry
        leg1.tp = tp
        leg1.sl = sl
        leg1.lots = lots
        leg1.ticket = ticket

        # Seed pair (if atomic)
        if is_atomic and seed_idx is not None:
             p2 = self._get_or_create_pair(group, seed_idx)
             seed_dir = seed_type or ("SELL" if trade_type == "BUY" else "BUY")
             leg2 = p2.buy_leg if seed_dir == "BUY" else p2.sell_leg
             leg2.status = "ACTIVE"
             leg2.entry = seed_entry or entry
             leg2.tp = seed_tp or 0
             leg2.sl = seed_sl or 0
             leg2.lots = lots
             leg2.ticket = seed_ticket

        # Log event
        atomic_str = "ATOMIC" if is_atomic else "NON-ATOMIC"
        msg = f"[{atomic_str}] {trade_type[0]}{pair_idx}"
        if is_atomic and seed_idx is not None:
            msg += f" + {seed_dir[0]}{seed_idx}"
            
        event = {
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "type": expansion_type,
            "message": msg,
            "details": f"C={c_count}, Entry={entry:.2f}"
        }
        group.events.append(event)
        self._write_event(group_id, event)

    def log_retracement_expansion(self, group_id: int, direction: str,
                                   level: int, target_price: float,
                                   s_idx: int, b_idx: int,
                                   s_entry: float, b_entry: float,
                                   s_tp: float, b_tp: float,
                                   s_sl: float, b_sl: float,
                                   lots: float, c_count: int,
                                   is_atomic: bool = True,
                                   s_ticket: int = 0, b_ticket: int = 0):
        """Log retracement-based expansion."""
        group = self._get_or_create_group(group_id)
        group.c_count = c_count

        if is_atomic or direction == "BEARISH":
            p_sell = self._get_or_create_pair(group, s_idx)
            p_sell.sell_leg.status = "ACTIVE"
            p_sell.sell_leg.entry = s_entry
            p_sell.sell_leg.tp = s_tp
            p_sell.sell_leg.sl = s_sl
            p_sell.sell_leg.lots = lots
            p_sell.sell_leg.ticket = s_ticket

        if is_atomic or direction == "BULLISH":
            p_buy = self._get_or_create_pair(group, b_idx)
            p_buy.buy_leg.status = "ACTIVE"
            p_buy.buy_leg.entry = b_entry
            p_buy.buy_leg.tp = b_tp
            p_buy.buy_leg.sl = b_sl
            p_buy.buy_leg.lots = lots
            p_buy.buy_leg.ticket = b_ticket

        event = {
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "type": "RETRACEMENT",
            "message": f"{direction} retracement L{level} @ {target_price:.2f}",
            "details": f"C={c_count}"
        }
        group.events.append(event)
        self._write_event(group_id, event)

    def log_tp_hit(self, group_id: int, pair_idx: int, leg: str,
                   price: float, was_incomplete: bool = False):
        """Log TP hit event."""
        group = self._get_or_create_group(group_id)
        if pair_idx in group.pairs:
            p = group.pairs[pair_idx]
            l = p.buy_leg if leg in ["BUY", "B"] else p.sell_leg
            l.status = "TP"

        incomplete_str = " (INCOMPLETE)" if was_incomplete else ""
        event = {
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "type": "TP",
            "message": f"{leg}{pair_idx} hit TP @ {price:.2f}{incomplete_str}",
            "details": f"Group={group_id}"
        }
        group.events.append(event)
        self._write_event(group_id, event)

    def log_sl_hit(self, group_id: int, pair_idx: int, leg: str, price: float):
        """Log SL hit event."""
        group = self._get_or_create_group(group_id)
        if pair_idx in group.pairs:
             p = group.pairs[pair_idx]
             l = p.buy_leg if leg in ["BUY", "B"] else p.sell_leg
             l.status = "SL"

        event = {
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "type": "SL",
            "message": f"{leg}{pair_idx} hit SL @ {price:.2f}",
            "details": f"Group={group_id}"
        }
        group.events.append(event)
        self._write_event(group_id, event)

    def log_non_atomic_complete(self, group_id: int, pair_idx: int,
                                 leg: str, entry: float, reason: str = "INIT_COMPLETE"):
        """Log non-atomic completing leg fired with INIT."""
        group = self._get_or_create_group(group_id)
        # Assuming this sets the leg to ACTIVE
        p = self._get_or_create_pair(group, pair_idx)
        l = p.buy_leg if leg in ["BUY", "B"] else p.sell_leg
        l.status = "ACTIVE"
        l.entry = entry

        event = {
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "type": "NON_ATOMIC_COMPLETE",
            "message": f"{leg}{pair_idx} @ {entry:.2f} ({reason})",
            "details": f"Completing previous group pair"
        }
        group.events.append(event)
        self._write_event(group_id, event)

    def update_pair(self, group_id: int, pair_idx: int,
                    trade_type: str = None, entry: float = None,
                    tp: float = None, sl: float = None,
                    re_entries: int = None, lots: float = None,
                    status: str = None, ticket: int = None):
        """Update specific fields of a pair LEG."""
        group = self._get_or_create_group(group_id)
        p = self._get_or_create_pair(group, pair_idx)
        
        # If trade_type is provided, update that leg. 
        # If NOT provided, we might be calling generically? 
        # SymbolEngine should pass trade_type for updates.
        
        if trade_type:
            l = p.buy_leg if trade_type in ["BUY", "B"] else p.sell_leg
            if entry is not None: l.entry = entry
            if tp is not None: l.tp = tp
            if sl is not None: l.sl = sl
            if re_entries is not None: l.re_entries = re_entries
            if lots is not None: l.lots = lots
            if status is not None: l.status = status
            if ticket is not None: l.ticket = ticket

    def update_c_count(self, group_id: int, c_count: int):
        """Update C count for a group."""
        group = self._get_or_create_group(group_id)
        group.c_count = c_count


    def render_group_table(self, group_id: int, current_price: float = 0.0) -> List[str]:
        """
        Render a formatted table for a single group as a list of strings.
        """
        if group_id not in self.groups:
            return [f"Group {group_id}: No data"]

        group = self.groups[group_id]
        lines = []

        # Header
        width = 110 # Expanded width
        header_line = self.ROW_CHAR * width
        
        # Group Status Header
        status_info = f"C={group.c_count}"
        if group.settled:
            status_info += " | SETTLED"
            
        title = f" [GROUP {group_id}] {group.init_direction} INIT @ {group.anchor:.2f} | Retrace: {group.pending_retracement} | {status_info}"
        lines.append(title)
        lines.append(header_line)

        # Column headers
        # Seq | Leg | Status | Entry | TP | SL | Lot | Notes
        col_header = (
            f" {'Leg':<6} {self.COL_SEP}"
            f" {'Status':<10} {self.COL_SEP}"
            f" {'Entry':>10} {self.COL_SEP}"
            f" {'TP':>10} {self.COL_SEP}"
            f" {'SL':>10} {self.COL_SEP}"
            f" {'Lots':>6} {self.COL_SEP}"
            f" {'Re':>3}"
        )
        lines.append(col_header)
        lines.append(header_line)

        # Sort pairs by index
        sorted_pairs = sorted(group.pairs.items(), key=lambda x: x[0])

        for pair_idx, pair in sorted_pairs:
            # Render BUY Leg
            leg_b = pair.buy_leg
            
            row_b = (
                f" B{pair_idx:<5} {self.COL_SEP}"
                f" {leg_b.status:<10} {self.COL_SEP}"
                f" {leg_b.entry:>10.2f} {self.COL_SEP}"
                f" {leg_b.tp:>10.2f} {self.COL_SEP}"
                f" {leg_b.sl:>10.2f} {self.COL_SEP}"
                f" {leg_b.lots:>6.2f} {self.COL_SEP}"
                f" {leg_b.re_entries:>3}"
            )
            lines.append(row_b)

            # Render SELL Leg
            leg_s = pair.sell_leg
            
            row_s = (
                f" S{pair_idx:<5} {self.COL_SEP}"
                f" {leg_s.status:<10} {self.COL_SEP}"
                f" {leg_s.entry:>10.2f} {self.COL_SEP}"
                f" {leg_s.tp:>10.2f} {self.COL_SEP}"
                f" {leg_s.sl:>10.2f} {self.COL_SEP}"
                f" {leg_s.lots:>6.2f} {self.COL_SEP}"
                f" {leg_s.re_entries:>3}"
            )
            lines.append(row_s)
            
            # Separator between pairs for readability? Optional.
            # lines.append(self.ROW_CHAR * width)

        lines.append(header_line)
        
        # Activity Log for this group
        lines.append(f" [GROUP {group_id} ACTIVITY]")
        if not group.events:
             lines.append(" (No events)")
        else:
             for event in group.events[-10:]: # Last 10 events to keep it readable
                 lines.append(f" {event['time']} | {event['type']:<15} | {event['message']}")
        
        lines.append(header_line)
        return lines

    def render_full_log(self, current_price: float = 0.0) -> str:
        """Render the complete log file content."""
        width = 110
        master_lines = []
        master_lines.append(self.HEADER_CHAR * width)
        
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header_info = f" SYMBOL: {self.symbol:<10}  PRICE: {current_price:<10.2f}  TIME: {ts}"
        master_lines.append(header_info.center(width))
        master_lines.append(self.HEADER_CHAR * width)
        master_lines.append("")
        
        # Render each group
        if not self.groups:
            master_lines.append(" No groups initialized.")
        else:
            for group_id in sorted(self.groups.keys()):
                group_lines = self.render_group_table(group_id, current_price)
                master_lines.extend(group_lines)
                master_lines.append("") # Spacing
                
        return "\n".join(master_lines)

    def _write_event(self, group_id: int, event: Dict[str, Any]):
        """Write event to log files."""
        timestamp = event["time"]
        event_type = event["type"]
        message = event["message"]
        details = event.get("details", "")

        log_line = f"[{timestamp}] [{event_type}] {message}"
        if details:
            log_line += f" | {details}"
        log_line += "\n"

        # Write to main log (Persistent History)
        with open(self.main_log_path, "a", encoding="utf-8") as f:
            f.write(log_line)

    def update_log_file(self, current_price: float = 0.0):
        """Update the main single log file with latest state."""
        content = self.render_full_log(current_price)
        
        # Overwrite mode - we want the file to represent CURRENT state
        # The user said "all the tables should and MUST be in one file"
        # and "in tabular manner". State snapshot is best for this.
        
        # Use a fixed filename for the session so it doesn't rotate endlessly
        # "groups_table_{session_id}.txt"
        
        filename = f"groups_log_{self.session_id}.txt"
        path = os.path.join(self.log_dir, filename)
        
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            print(f"Error writing group log: {e}")

    # Legacy/Aliases
    def write_raw_group_table(self, group_id, content):
        pass # Deprecated by update_log_file


    def print_group_table(self, group_id: int):
        """Print group table to console."""
        print(self.render_group_table(group_id))

    def print_all_groups(self):
        """Print all group tables to console."""
        print(self.render_all_groups())

    def get_group_data(self, group_id: int) -> Optional[GroupData]:
        """Get raw group data for external use."""
        return self.groups.get(group_id)

    def get_pending_retracement(self, group_id: int) -> Optional[str]:
        """Get pending retracement direction for a group."""
        group = self.groups.get(group_id)
        return group.pending_retracement if group else None

    def get_init_direction(self, group_id: int) -> Optional[str]:
        """Get init direction for a group."""
        group = self.groups.get(group_id)
        return group.init_direction if group else None
