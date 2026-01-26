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
class PairData:
    """Data for a single pair within a group."""
    pair_idx: int
    trade_type: str = ""  # "BUY" or "SELL"
    entry: float = 0.0
    tp: float = 0.0
    sl: float = 0.0
    re_entries: int = 0
    lots: float = 0.0
    status: str = "PENDING"  # PENDING, ACTIVE, TP, SL, CLOSED
    ticket: int = 0


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

    Creates readable table-formatted logs per group with:
    - Pair details: Type, Entry, TP, SL, Re-entries, Lots, Status
    - Group status: C count, Anchor, Direction, Retracement direction
    - Event history
    """

    # Table formatting constants
    HEADER_CHAR = "═"
    ROW_CHAR = "─"
    COL_SEP = "│"

    def __init__(self, symbol: str, log_dir: str = "logs", user_id: str = None):
        """
        Initialize the group logger.

        Args:
            symbol: Trading symbol (e.g., "BTCUSD")
            log_dir: Base directory for log files
            user_id: User ID for per-user logging (matches SessionLogger structure)
        """
        self.symbol = symbol
        self.user_id = user_id
        self.groups: Dict[int, GroupData] = {}
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Determine log directory based on user_id
        # If user_id provided, use same structure as SessionLogger: logs/users/{user_id}/sessions/
        # Otherwise use logs/ directly for debugging
        from pathlib import Path
        root_dir = Path(__file__).resolve().parent.parent.parent

        if user_id:
            self.log_dir = root_dir / "logs" / "users" / user_id / "sessions"
        else:
            self.log_dir = root_dir / log_dir

        # Ensure log directory exists
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

    def log_init(self, group_id: int, anchor: float, is_bullish_source: bool,
                 b_idx: int, s_idx: int, b_ticket: int = 0, s_ticket: int = 0,
                 b_entry: float = 0, s_entry: float = 0,
                 b_tp: float = 0, s_tp: float = 0,
                 b_sl: float = 0, s_sl: float = 0,
                 lots: float = 0.01):
        """
        Log group initialization.

        Args:
            group_id: The group being initialized
            anchor: Anchor price for the group
            is_bullish_source: True if INIT was caused by bullish TP
            b_idx: Buy pair index
            s_idx: Sell pair index
            b_ticket: Buy ticket number
            s_ticket: Sell ticket number
        """
        group = self._get_or_create_group(group_id)
        group.anchor = anchor
        group.init_direction = "BULLISH" if is_bullish_source else "BEARISH"
        # Pending retracement is OPPOSITE of init direction
        group.pending_retracement = "BEARISH" if is_bullish_source else "BULLISH"
        group.c_count = 0
        group.settled = False

        # Add the INIT pairs
        group.pairs[b_idx] = PairData(
            pair_idx=b_idx,
            trade_type="BUY",
            entry=b_entry if b_entry else anchor,
            tp=b_tp,
            sl=b_sl,
            lots=lots,
            status="ACTIVE",
            ticket=b_ticket
        )
        group.pairs[s_idx] = PairData(
            pair_idx=s_idx,
            trade_type="SELL",
            entry=s_entry if s_entry else anchor,
            tp=s_tp,
            sl=s_sl,
            lots=lots,
            status="ACTIVE",
            ticket=s_ticket
        )

        # Log event
        event = {
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "type": "INIT",
            "message": f"Group {group_id} INIT @ {anchor:.2f} ({group.init_direction} source)",
            "details": f"B{b_idx}+S{s_idx}, Pending retracement: {group.pending_retracement}"
        }
        group.events.append(event)

        self._write_event(group_id, event)
        # self._write_group_table(group_id)

    def log_expansion(self, group_id: int, expansion_type: str,
                      pair_idx: int, trade_type: str, entry: float,
                      tp: float, sl: float, lots: float, ticket: int = 0,
                      seed_idx: int = None, seed_type: str = None,
                      seed_entry: float = None, seed_tp: float = None,
                      seed_sl: float = None, seed_ticket: int = 0,
                      is_atomic: bool = True, c_count: int = 0):
        """
        Log grid expansion (atomic or non-atomic).

        Args:
            group_id: Group being expanded
            expansion_type: "TP_EXPAND", "STEP_EXPAND", "RETRACEMENT"
            pair_idx: Completing pair index
            trade_type: "BUY" or "SELL"
            entry, tp, sl, lots: Trade details
            seed_idx: New seeded pair index (None for non-atomic)
            is_atomic: True if both legs fired, False for single leg
            c_count: Current C count after expansion
        """
        group = self._get_or_create_group(group_id)
        group.c_count = c_count

        # Update/add completing pair
        if pair_idx not in group.pairs:
            group.pairs[pair_idx] = PairData(pair_idx=pair_idx)

        pair = group.pairs[pair_idx]
        pair.trade_type = trade_type
        pair.entry = entry
        pair.tp = tp
        pair.sl = sl
        pair.lots = lots
        pair.status = "ACTIVE"
        pair.ticket = ticket

        # Add seeded pair if atomic
        if is_atomic and seed_idx is not None:
            group.pairs[seed_idx] = PairData(
                pair_idx=seed_idx,
                trade_type=seed_type or ("SELL" if trade_type == "BUY" else "BUY"),
                entry=seed_entry or entry,
                tp=seed_tp or 0,
                sl=seed_sl or 0,
                lots=lots,
                status="ACTIVE",
                ticket=seed_ticket
            )

        # Log event
        atomic_str = "ATOMIC" if is_atomic else "NON-ATOMIC"
        if is_atomic and seed_idx is not None:
            msg = f"[{atomic_str}] {trade_type[0]}{pair_idx} + {seed_type[0] if seed_type else ('S' if trade_type == 'BUY' else 'B')}{seed_idx}"
        else:
            msg = f"[{atomic_str}] {trade_type[0]}{pair_idx} only"

        event = {
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "type": expansion_type,
            "message": msg,
            "details": f"C={c_count}, Entry={entry:.2f}"
        }
        group.events.append(event)

        self._write_event(group_id, event)
        # self._write_group_table(group_id)

    def log_retracement_expansion(self, group_id: int, direction: str,
                                   level: int, target_price: float,
                                   s_idx: int, b_idx: int,
                                   s_entry: float, b_entry: float,
                                   s_tp: float, b_tp: float,
                                   s_sl: float, b_sl: float,
                                   lots: float, c_count: int,
                                   is_atomic: bool = True,
                                   s_ticket: int = 0, b_ticket: int = 0):
        """
        Log retracement-based expansion after INIT.

        Args:
            group_id: Group being expanded
            direction: "BULLISH" or "BEARISH" retracement
            level: Retracement level number (1, 2, 3...)
            target_price: Price that triggered retracement
        """
        group = self._get_or_create_group(group_id)
        group.c_count = c_count

        # Add pairs
        if is_atomic or direction == "BEARISH":
            group.pairs[s_idx] = PairData(
                pair_idx=s_idx,
                trade_type="SELL",
                entry=s_entry,
                tp=s_tp,
                sl=s_sl,
                lots=lots,
                status="ACTIVE",
                ticket=s_ticket
            )

        if is_atomic or direction == "BULLISH":
            group.pairs[b_idx] = PairData(
                pair_idx=b_idx,
                trade_type="BUY",
                entry=b_entry,
                tp=b_tp,
                sl=b_sl,
                lots=lots,
                status="ACTIVE",
                ticket=b_ticket
            )

        atomic_str = "ATOMIC" if is_atomic else "NON-ATOMIC"
        if is_atomic:
            msg = f"[{atomic_str}] S{s_idx} + B{b_idx}"
        else:
            if direction == "BEARISH":
                msg = f"[{atomic_str}] S{s_idx} only"
            else:
                msg = f"[{atomic_str}] B{b_idx} only"

        event = {
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "type": "RETRACEMENT",
            "message": f"{direction} retracement L{level} @ {target_price:.2f}",
            "details": f"{msg}, C={c_count}"
        }
        group.events.append(event)

        self._write_event(group_id, event)
        # self._write_group_table(group_id)

    def log_tp_hit(self, group_id: int, pair_idx: int, leg: str,
                   price: float, was_incomplete: bool = False):
        """Log TP hit event."""
        group = self._get_or_create_group(group_id)

        if pair_idx in group.pairs:
            group.pairs[pair_idx].status = "TP"

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
            group.pairs[pair_idx].status = "SL"

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
        """Update specific fields of a pair."""
        group = self._get_or_create_group(group_id)

        if pair_idx not in group.pairs:
            group.pairs[pair_idx] = PairData(pair_idx=pair_idx)

        pair = group.pairs[pair_idx]
        if trade_type is not None:
            pair.trade_type = trade_type
        if entry is not None:
            pair.entry = entry
        if tp is not None:
            pair.tp = tp
        if sl is not None:
            pair.sl = sl
        if re_entries is not None:
            pair.re_entries = re_entries
        if lots is not None:
            pair.lots = lots
        if status is not None:
            pair.status = status
        if ticket is not None:
            pair.ticket = ticket

    def update_c_count(self, group_id: int, c_count: int):
        """Update C count for a group."""
        group = self._get_or_create_group(group_id)
        group.c_count = c_count

    def render_group_table(self, group_id: int) -> str:
        """
        Render a formatted table for a single group.

        Returns:
            Formatted string with group table
        """
        if group_id not in self.groups:
            return f"Group {group_id}: No data"

        group = self.groups[group_id]
        lines = []

        # Header
        width = 95
        header_line = self.HEADER_CHAR * width
        lines.append(header_line)

        title = f"GROUP {group_id} - {group.init_direction} INIT @ {group.anchor:.2f}"
        lines.append(title.center(width))

        lines.append(header_line)

        # Column headers
        col_header = (
            f"{'Pair':<6}{self.COL_SEP}"
            f"{'Type':<6}{self.COL_SEP}"
            f"{'Entry':>12}{self.COL_SEP}"
            f"{'TP':>12}{self.COL_SEP}"
            f"{'SL':>12}{self.COL_SEP}"
            f"{'Re-entries':>10}{self.COL_SEP}"
            f"{'Lots':>8}{self.COL_SEP}"
            f"{'Status':<8}"
        )
        lines.append(col_header)

        # Separator
        sep_line = self.ROW_CHAR * width
        lines.append(sep_line)

        # Sort pairs by index
        sorted_pairs = sorted(group.pairs.items(), key=lambda x: x[0])

        for pair_idx, pair in sorted_pairs:
            prefix = "B" if pair.trade_type == "BUY" else "S"
            row = (
                f"{prefix}{pair_idx:<5}{self.COL_SEP}"
                f"{pair.trade_type:<6}{self.COL_SEP}"
                f"{pair.entry:>12.2f}{self.COL_SEP}"
                f"{pair.tp:>12.2f}{self.COL_SEP}"
                f"{pair.sl:>12.2f}{self.COL_SEP}"
                f"{pair.re_entries:>10}{self.COL_SEP}"
                f"{pair.lots:>8.2f}{self.COL_SEP}"
                f"{pair.status:<8}"
            )
            lines.append(row)

        # Footer with status
        lines.append(header_line)
        status_line = (
            f"C={group.c_count} {self.COL_SEP} "
            f"Pending Retracement: {group.pending_retracement} {self.COL_SEP} "
            f"Settled: {'Yes' if group.settled else 'No'}"
        )
        lines.append(status_line)
        lines.append(header_line)

        return "\n".join(lines)

    def render_all_groups(self) -> str:
        """Render tables for all groups."""
        if not self.groups:
            return "No groups initialized"

        tables = []
        for group_id in sorted(self.groups.keys()):
            tables.append(self.render_group_table(group_id))
            tables.append("")  # Empty line between groups

        return "\n".join(tables)

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

        # Write to main log
        with open(self.main_log_path, "a", encoding="utf-8") as f:
            f.write(log_line)

        # Write to group-specific log
        safe_symbol = self.symbol.replace(" ", "_").replace("/", "_")
        group_log_path = os.path.join(
            self.log_dir, f"group_{group_id}_{safe_symbol}_{self.session_id}.log"
        )
        with open(group_log_path, "a", encoding="utf-8") as f:
            f.write(log_line)

    def _write_group_table(self, group_id: int):
        """Write current group table state to file."""
        safe_symbol = self.symbol.replace(" ", "_").replace("/", "_")
        table_path = os.path.join(
            self.log_dir, f"group_{group_id}_{safe_symbol}_{self.session_id}_table.txt"
        )

        table = self.render_group_table(group_id)
        with open(table_path, "w", encoding="utf-8") as f:
            f.write(table)

    def write_raw_group_table(self, group_id: int, content: str):
        """Write raw content to the group table file (used by SymbolEngine)."""
        safe_symbol = self.symbol.replace(" ", "_").replace("/", "_")
        table_path = os.path.join(
            self.log_dir, f"group_{group_id}_{safe_symbol}_{self.session_id}_table.txt"
        )
        with open(table_path, "w", encoding="utf-8") as f:
            f.write(content)

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
