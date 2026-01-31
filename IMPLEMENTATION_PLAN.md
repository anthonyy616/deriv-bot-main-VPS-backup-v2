# Implementation Plan: Ancestor Blocking + Logging Enhancements

This plan contains exact line numbers and step-by-step instructions for implementing three fixes.

---

## Part 1: Block Ancestor Incomplete Pairs from Firing INIT

### Problem
Incomplete pairs from ancestor groups (`group_id < current_group - 1`) incorrectly fire INIT for new groups, causing:
- Unexpected group creation (e.g., S202 fires Group 5 instead of waiting)
- `current_group` changes prematurely
- Natural expansion for the actual current group gets skipped

### Location
**File:** `core/engine/symbol_engine.py`
**Function:** `_check_position_drops`
**Lines:** 3098-3113

### Current Code (Lines 3098-3113)
```python
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
```

### Fixed Code (Replace lines 3098-3113)
```python
if is_tp:
    if was_incomplete:
        # INCOMPLETE PAIR TP -> Fire INIT for next group
        # BUT ONLY if this pair belongs to current_group or current_group - 1

        # ANCESTOR BLOCK: Pairs from groups < current_group - 1 should NOT fire INIT
        # This prevents old groups from hijacking the current group's progression
        if group_id < self.current_group - 1:
            print(f"[TP-INCOMPLETE-BLOCKED] Pair={pair_idx} Group={group_id} is ANCESTOR (< {self.current_group - 1}), ignoring INIT trigger")
        elif pair_idx in self._incomplete_pairs_init_triggered:
            print(f"[TP-INCOMPLETE-BLOCKED] Pair={pair_idx} already fired INIT before, skipping")
        elif self.graceful_stop:
            print(f"[TP-INCOMPLETE] Pair={pair_idx} Group={group_id} -> graceful stop active, no INIT")
        else:
            print(f"[TP-INCOMPLETE] Pair={pair_idx} Group={group_id} -> Firing INIT for Group {self.current_group + 1} (Bullish={is_bullish})")
            self._incomplete_pairs_init_triggered.add(pair_idx)

            # Pass triggering pair index so Init can fill the missing leg of previous group
            await self._execute_group_init(self.current_group + 1, event_price, is_bullish_source=is_bullish, trigger_pair_idx=pair_idx)
```

### Key Change
Added this check as the FIRST condition:
```python
if group_id < self.current_group - 1:
    print(f"[TP-INCOMPLETE-BLOCKED] Pair={pair_idx} Group={group_id} is ANCESTOR (< {self.current_group - 1}), ignoring INIT trigger")
```

This ensures:
- `current_group - 1` CAN fire INIT (the parent group)
- `current_group` CAN fire INIT (the active group)
- Groups `< current_group - 1` are BLOCKED (ancestor groups)

---

## Part 2: Lot Size Progression Logging

### 2.1 Add Fields to GridPair Class

**File:** `core/engine/symbol_engine.py`
**Location:** GridPair class, after line 97 (after `tp_blocked` field)

**Add these fields:**
```python
    # Lot size history for progression tracking
    buy_lot_history: List[float] = field(default_factory=list)
    sell_lot_history: List[float] = field(default_factory=list)
```

**Note:** Also add `List` to the imports at line 17:
```python
from typing import Dict, Optional, List, Any, Set
```
(List is already imported, so no change needed)

### 2.2 Track Lot Sizes on Order Execution

**File:** `core/engine/symbol_engine.py`
**Function:** `_execute_market_order`
**Location:** After line 4313 (after the locked entry price setting), before `return position_ticket`

**Add this code after line 4313:**
```python
                # Track lot size history for progression logging
                if direction == "buy":
                    pair.buy_lot_history.append(volume)
                elif direction == "sell":
                    pair.sell_lot_history.append(volume)
```

The full block (lines 4307-4315) will look like:
```python
            if pair:
                if direction == "buy" and pair.locked_buy_entry == 0.0:
                    pair.locked_buy_entry = exec_price
                    print(f"[LOCKED] Pair {index} BUY entry locked at {exec_price:.2f}")
                elif direction == "sell" and pair.locked_sell_entry == 0.0:
                    pair.locked_sell_entry = exec_price
                    print(f"[LOCKED] Pair {index} SELL entry locked at {exec_price:.2f}")

                # Track lot size history for progression logging
                if direction == "buy":
                    pair.buy_lot_history.append(volume)
                elif direction == "sell":
                    pair.sell_lot_history.append(volume)

            return position_ticket
```

### 2.3 Update GroupLogger Table Display

**File:** `core/engine/group_logger.py`

#### 2.3.1 Update PairLegData class (lines 18-27)
Add a lot_history field:
```python
@dataclass
class PairLegData:
    """Data for a single leg (Buy or Sell) of a pair."""
    status: str = "PENDING"
    entry: float = 0.0
    tp: float = 0.0
    sl: float = 0.0
    lots: float = 0.0
    ticket: int = 0
    re_entries: int = 0
    lot_history: List[float] = field(default_factory=list)  # NEW
```

**Note:** Add imports at the top of the file (after line 4):
```python
from typing import Dict, List, Optional, Any
```
(List is already imported, no change needed)

#### 2.3.2 Update Column Headers (lines 327-335)
Change the column header to show "Lot Progression":
```python
        col_header = (
            f" {'Leg':<6} {self.COL_SEP}"
            f" {'Status':<10} {self.COL_SEP}"
            f" {'Entry':>10} {self.COL_SEP}"
            f" {'TP':>10} {self.COL_SEP}"
            f" {'SL':>10} {self.COL_SEP}"
            f" {'Lot Progression':>20}"
        )
```

#### 2.3.3 Add Helper Function (before render_group_table, around line 301)
```python
    def _format_lot_progression(self, lot_history: List[float]) -> str:
        """Format lot history as progression string."""
        if not lot_history:
            return "0.00"
        return " -> ".join(f"{lot:.2f}" for lot in lot_history)
```

#### 2.3.4 Update Row Rendering (lines 342-369)
Change the lot display to show progression:
```python
        for pair_idx, pair in sorted_pairs:
            # Render BUY Leg
            leg_b = pair.buy_leg
            lot_prog_b = self._format_lot_progression(leg_b.lot_history) if leg_b.lot_history else f"{leg_b.lots:.2f}"

            row_b = (
                f" B{pair_idx:<5} {self.COL_SEP}"
                f" {leg_b.status:<10} {self.COL_SEP}"
                f" {leg_b.entry:>10.2f} {self.COL_SEP}"
                f" {leg_b.tp:>10.2f} {self.COL_SEP}"
                f" {leg_b.sl:>10.2f} {self.COL_SEP}"
                f" {lot_prog_b:>20}"
            )
            lines.append(row_b)

            # Render SELL Leg
            leg_s = pair.sell_leg
            lot_prog_s = self._format_lot_progression(leg_s.lot_history) if leg_s.lot_history else f"{leg_s.lots:.2f}"

            row_s = (
                f" S{pair_idx:<5} {self.COL_SEP}"
                f" {leg_s.status:<10} {self.COL_SEP}"
                f" {leg_s.entry:>10.2f} {self.COL_SEP}"
                f" {leg_s.tp:>10.2f} {self.COL_SEP}"
                f" {leg_s.sl:>10.2f} {self.COL_SEP}"
                f" {lot_prog_s:>20}"
            )
            lines.append(row_s)
```

#### 2.3.5 Update log_tp_hit to Include Lot History (lines 226-243)
```python
    def log_tp_hit(self, group_id: int, pair_idx: int, leg: str,
                   price: float, was_incomplete: bool = False, lot_history: List[float] = None):
        """Log TP hit event with lot history."""
        group = self._get_or_create_group(group_id)
        if pair_idx in group.pairs:
            p = group.pairs[pair_idx]
            l = p.buy_leg if leg in ["BUY", "B"] else p.sell_leg
            l.status = "TP"

        # Build lot string if history provided
        lot_str = ""
        if lot_history:
            lot_str = f" | Lots: [{', '.join(f'{l:.2f}' for l in lot_history)}] Total: {sum(lot_history):.2f}"

        incomplete_str = " (INCOMPLETE)" if was_incomplete else ""
        event = {
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "type": "TP",
            "message": f"{leg}{pair_idx} hit TP @ {price:.2f}{lot_str}{incomplete_str}",
            "details": f"Group={group_id}"
        }
        group.events.append(event)
        self._write_event(group_id, event)
```

#### 2.3.6 Update update_pair to Accept lot_history (lines 273-294)
```python
    def update_pair(self, group_id: int, pair_idx: int,
                    trade_type: str = None, entry: float = None,
                    tp: float = None, sl: float = None,
                    re_entries: int = None, lots: float = None,
                    status: str = None, ticket: int = None,
                    lot_history: List[float] = None):  # NEW parameter
        """Update specific fields of a pair LEG."""
        group = self._get_or_create_group(group_id)
        p = self._get_or_create_pair(group, pair_idx)

        if trade_type:
            l = p.buy_leg if trade_type in ["BUY", "B"] else p.sell_leg
            if entry is not None: l.entry = entry
            if tp is not None: l.tp = tp
            if sl is not None: l.sl = sl
            if re_entries is not None: l.re_entries = re_entries
            if lots is not None: l.lots = lots
            if status is not None: l.status = status
            if ticket is not None: l.ticket = ticket
            if lot_history is not None: l.lot_history = lot_history  # NEW
```

---

## Part 3: Comprehensive Trading Activity Log

### 3.1 Create Activity Logger in SymbolEngine.__init__

**File:** `core/engine/symbol_engine.py`
**Location:** In `__init__`, after line 475 (after toggle logger setup)

**Add this code:**
```python
        # --- TRADING ACTIVITY LOGGER (Clean log without FastAPI noise) ---
        self.activity_logger = logging.getLogger('trading_activity')
        self.activity_logger.propagate = False  # Prevent terminal spam
        if not self.activity_logger.handlers:
            # Ensure logs directory exists
            os.makedirs('logs', exist_ok=True)
            activity_handler = logging.FileHandler('logs/trading_activity.log')
            activity_handler.setFormatter(logging.Formatter('[%(asctime)s.%(msecs)03d] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
            self.activity_logger.addHandler(activity_handler)
            self.activity_logger.setLevel(logging.INFO)
```

### 3.2 Add Helper Method for Activity Logging

**File:** `core/engine/symbol_engine.py`
**Location:** After `__init__`, around line 530 (before `@property` methods)

**Add this method:**
```python
    def _log_activity(self, event_type: str, message: str):
        """Log trading activity to dedicated log file."""
        self.activity_logger.info(f"[{event_type}] {message}")
```

### 3.3 Add Activity Logs Throughout the Code

#### 3.3.1 Log INIT Events
**Location:** In `_execute_group_init` (find this function)
After group creation, add:
```python
self._log_activity("INIT", f"Group {group_id} created @ {anchor:.2f} ({direction} source) -> B{b_idx} + S{s_idx}")
```

#### 3.3.2 Log ORDER Events
**Location:** In `_execute_market_order`, after successful order (around line 4288)
Replace or augment the existing print with:
```python
self._log_activity("ORDER", f"{leg}{index} OPEN @ {exec_price:.2f} | Lot: {volume:.2f} | TP: {tp:.2f} | SL: {sl:.2f}")
```

#### 3.3.3 Log STEP_EXPAND Events
**Location:** In step expansion functions (search for "STEP_EXPAND" in prints)
Add after each expansion:
```python
self._log_activity("STEP_EXPAND", f"Group {self.current_group} | Price crossed spread level -> B{b_idx} + S{s_idx}")
```

#### 3.3.4 Log TP_EXPAND Events
**Location:** In `_execute_tp_expansion` function
Add:
```python
self._log_activity("TP_EXPAND", f"{trigger_leg}{trigger_idx} hit TP @ {price:.2f} -> Triggered B{new_b_idx} + S{new_s_idx}")
```

#### 3.3.5 Log TP Hit Events
**Location:** In `_check_position_drops`, after TP hit detection (around line 3127)
Add:
```python
self._log_activity("TP", f"{leg}{pair_idx} hit TP @ {event_price:.2f} {'(INCOMPLETE)' if was_incomplete else ''}")
```

#### 3.3.6 Log BLOCK Events
**Location:** Wherever blocking occurs, add appropriate logs:

For ancestor blocking (Part 1 fix):
```python
self._log_activity("TP-BLOCKED", f"{leg}{pair_idx} (Group {group_id}) hit TP @ {event_price:.2f} - ANCESTOR GROUP BLOCKED ({group_id} < {self.current_group - 1})")
```

For duplicate expansion blocking:
```python
self._log_activity("BLOCK", f"Pair {pair_idx} already fired expansion, blocked")
```

For TP blocking (re-entry prevention):
```python
self._log_activity("BLOCK", f"Pair {pair_idx} blocked from re-entry (TP hit)")
```

#### 3.3.7 Log TOGGLE Events
**Location:** In toggle trigger code (search for "TOGGLE" or "toggle_trigger")
Add:
```python
self._log_activity("TOGGLE", f"Pair {pair_idx} | {direction.upper()} @ {price:.2f} | trade_count: {old_count} -> {new_count} | Lot: {volume:.2f}")
```

---

## Summary Checklist

### Part 1: Ancestor Blocking
- [ ] Edit `_check_position_drops` at lines 3098-3113
- [ ] Add ancestor group check: `if group_id < self.current_group - 1`
- [ ] Add appropriate log message for blocked ancestors

### Part 2: Lot Size Progression
- [ ] Add `buy_lot_history` and `sell_lot_history` to GridPair class (line 97)
- [ ] Track lot sizes in `_execute_market_order` (after line 4313)
- [ ] Add `lot_history` field to PairLegData in group_logger.py
- [ ] Add `_format_lot_progression` helper method
- [ ] Update column headers in render_group_table
- [ ] Update row rendering to show lot progression
- [ ] Update `log_tp_hit` to accept and display lot_history
- [ ] Update `update_pair` to accept lot_history parameter

### Part 3: Trading Activity Log
- [ ] Add `activity_logger` in `__init__` (after line 475)
- [ ] Add `_log_activity` helper method
- [ ] Add activity logs at key decision points:
  - [ ] INIT events
  - [ ] ORDER events
  - [ ] STEP_EXPAND events
  - [ ] TP_EXPAND events
  - [ ] TP hit events
  - [ ] BLOCK events (all types)
  - [ ] TOGGLE events

---

## Files Modified

1. **core/engine/symbol_engine.py**
   - GridPair class: Add lot history fields
   - `__init__`: Add activity_logger
   - `_log_activity`: New helper method
   - `_execute_market_order`: Track lot history
   - `_check_position_drops`: Add ancestor blocking

2. **core/engine/group_logger.py**
   - PairLegData: Add lot_history field
   - `_format_lot_progression`: New helper method
   - `render_group_table`: Update column headers and row rendering
   - `log_tp_hit`: Add lot_history parameter
   - `update_pair`: Add lot_history parameter
