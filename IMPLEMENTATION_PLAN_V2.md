# Implementation Plan V2: Critical Bug Fixes

This plan addresses 6 bugs found during code review. All changes are in `core/engine/symbol_engine.py` unless otherwise noted.

---

## Bug 1: `_handle_completed_pair_expansion` Uses LIVE C Instead of HIGHWATER

### Problem
When positions close via SL, the live C count drops and TP expansion fires AGAIN. This caused Group 2 to expand all the way to S203 when it should have stopped at C=2.

### Location
**Line 2981** in `_handle_completed_pair_expansion`

### Current Code
```python
async def _handle_completed_pair_expansion(self, event_price: float, is_bullish: bool):
    """
    Handle expansion in active group driven by prior group TP.
    """
    group_id = self.current_group
    C = self._count_completed_pairs_for_group(group_id)  # <-- BUG: Uses live count
    if C >= 3:
         return
    await self._execute_tp_expansion(group_id, event_price, is_bullish, C)
```

### Fixed Code
```python
async def _handle_completed_pair_expansion(self, event_price: float, is_bullish: bool):
    """
    Handle expansion in active group driven by prior group TP.
    """
    group_id = self.current_group
    C = self._get_c_highwater(group_id)  # <-- FIX: Use highwater instead of live count

    if C >= 3:
         return

    # For groups > 0, stop TP expansion at C >= 2
    if group_id > 0 and C >= 2:
        print(f"[PRIOR-TP-DRIVER] BLOCKED: Group {group_id} C_highwater={C} >= 2")
        return

    await self._execute_tp_expansion(group_id, event_price, is_bullish, C)
```

---

## Bug 2: Non-Atomic Step Expansion Fires for Groups > 0

### Problem
The non-atomic expansion at C==2 fires for ALL groups, but it should ONLY fire for Group 0. Groups > 0 should be blocked at C >= 2.

### Location
**Lines 1183-1247** in `_expand_bullish`
**Lines 1303-1367** in `_expand_bearish`

### Fix for `_expand_bullish` (around line 1183)

**Current Code:**
```python
async with self.execution_lock:
    # Use High-Water C for gating
    C = self._get_c_highwater(self.current_group)
    if C >= 3:
        print(f"[EXPAND-BULL] BLOCKED C={C} >= 3")
        return
```

**Fixed Code:**
```python
async with self.execution_lock:
    # Use High-Water C for gating
    C = self._get_c_highwater(self.current_group)
    if C >= 3:
        print(f"[EXPAND-BULL] BLOCKED C={C} >= 3")
        return

    # For groups > 0, block natural expansion at C >= 2
    if self.current_group > 0 and C >= 2:
        print(f"[EXPAND-BULL] BLOCKED: Group {self.current_group} C={C} >= 2 (non-atomic only for Group 0)")
        return
```

### Fix for `_expand_bearish` (around line 1303)

**Current Code:**
```python
async with self.execution_lock:
    # Use High-Water C for gating
    C = self._get_c_highwater(self.current_group)
    if C >= 3:
        print(f"[EXPAND-BEAR] BLOCKED C={C} >= 3")
        return
```

**Fixed Code:**
```python
async with self.execution_lock:
    # Use High-Water C for gating
    C = self._get_c_highwater(self.current_group)
    if C >= 3:
        print(f"[EXPAND-BEAR] BLOCKED C={C} >= 3")
        return

    # For groups > 0, block natural expansion at C >= 2
    if self.current_group > 0 and C >= 2:
        print(f"[EXPAND-BEAR] BLOCKED: Group {self.current_group} C={C} >= 2 (non-atomic only for Group 0)")
        return
```

---

## Bug 3: TP Expansion at C==2 Fires for Groups > 0

### Problem
In `_execute_tp_expansion`, when C==2, non-atomic TP expansion fires for ALL groups. It should only fire for Group 0.

### Location
**Lines 2759-2791** in `_execute_tp_expansion` (bullish branch)
**Lines 2838-2870** in `_execute_tp_expansion` (bearish branch)

### Fix for Bullish Branch (around line 2759)

**Current Code:**
```python
if C == 2:
    print(f"[TP-EXPAND] C==2: B{complete_idx} only (Non-Atomic Fill)")
    await self._place_single_leg_tp("buy", tick.ask, complete_idx)
    # ... logging ...
    if group_id == 0:
        await self._force_artificial_tp_and_init(tick, event_price=event_price)
    else:
        print(f"[GROUP {group_id} TP-EXPAND] C=3 reached. Waiting for Incomplete Pair TP...")
```

**Fixed Code:**
```python
if C == 2:
    # For groups > 0, block at C >= 2
    if group_id > 0:
        print(f"[TP-EXPAND] BLOCKED: Group {group_id} C={C} >= 2 (non-atomic only for Group 0)")
        return

    # Only Group 0 gets non-atomic at C==2
    print(f"[TP-EXPAND] C==2: B{complete_idx} only (Non-Atomic Fill)")
    await self._place_single_leg_tp("buy", tick.ask, complete_idx)
    # ... rest of logging unchanged ...
    print(f"[GROUP 0 SATURATION] C=3 reached via TP Expansion. Forcing Artificial TP.")
    await self._force_artificial_tp_and_init(tick, event_price=event_price)
```

### Fix for Bearish Branch (around line 2826)

Apply the same pattern:
```python
if C == 2:
    # For groups > 0, block at C >= 2
    if group_id > 0:
        print(f"[TP-EXPAND] BLOCKED: Group {group_id} C={C} >= 2 (non-atomic only for Group 0)")
        return

    # Only Group 0 gets non-atomic at C==2
    print(f"[TP-EXPAND] C==2: S{complete_idx} only (Non-Atomic Fill)")
    # ... rest unchanged for Group 0 ...
```

---

## Bug 4: Toggle Triggers Don't Update group_logger with Entry Price

### Problem
When toggle trigger opens a new position (e.g., B3), the `group_logger.update_pair` is NOT called. This causes the entry price in the table to show 0.00 even though trades executed.

### Location
**Line 3718** in `_execute_trade_with_chain` (after `pair.advance_toggle()`)

### Fix
Add group_logger update after successful trade execution. Insert after line 3718:

```python
pair.record_position_open(ticket)
pair.advance_toggle()

# UPDATE GROUP LOGGER with entry price for toggle trades
if self.group_logger:
    # Get the actual fill price from locked entries
    if direction == "buy":
        actual_entry = pair.locked_buy_entry if pair.locked_buy_entry > 0 else pair.buy_price
        tp_price = actual_entry + self.buy_stop_tp_pips
        sl_price = actual_entry - self.buy_stop_sl_pips
    else:
        actual_entry = pair.locked_sell_entry if pair.locked_sell_entry > 0 else pair.sell_price
        tp_price = actual_entry - self.sell_stop_tp_pips
        sl_price = actual_entry + self.sell_stop_sl_pips

    # Get lot size used (it's in the lot_history now)
    if direction == "buy" and pair.buy_lot_history:
        lot_used = pair.buy_lot_history[-1]
    elif direction == "sell" and pair.sell_lot_history:
        lot_used = pair.sell_lot_history[-1]
    else:
        lot_used = self.lot_sizes[0] if self.lot_sizes else 0.01

    self.group_logger.update_pair(
        group_id=pair.group_id,
        pair_idx=pair_idx,
        trade_type=direction.upper(),
        entry=actual_entry,
        tp=tp_price,
        sl=sl_price,
        lots=lot_used,
        status="ACTIVE",
        ticket=ticket
    )
```

---

## Bug 5: Group 0 Incomplete TP Not Logged in Activity

### Problem
When an incomplete pair from Group 0 hits TP and triggers INIT for Group 1, the activity log doesn't show this event.

### Location
**Lines 3140-3163** in `_check_position_drops` (incomplete pair TP handling)

### Current Code
The activity logging only happens when INIT actually fires:
```python
if group_id < self.current_group - 1:
    print(f"[TP-INCOMPLETE-BLOCKED] ...")
    self._log_activity("TP-BLOCKED", ...)
elif pair_idx in self._incomplete_pairs_init_triggered:
    print(f"[TP-INCOMPLETE-BLOCKED] ...")
elif self.graceful_stop:
    print(f"[TP-INCOMPLETE] ...")
else:
    print(f"[TP-INCOMPLETE] ...")
    self._log_activity("TP", f"{leg}{pair_idx} hit TP @ {event_price:.2f} (INCOMPLETE) -> INIT Group {self.current_group + 1}")
    # ... execute init
```

### Fix
Add activity logging for ALL incomplete TP hits, including Group 0. Also log to group_logger.

Replace the incomplete pair TP handling block (lines 3140-3163) with:

```python
if is_tp:
    if was_incomplete:
        # LOG INCOMPLETE TP HIT to group_logger first (for ALL groups including 0)
        if self.group_logger:
            self.group_logger.log_tp_hit(
                group_id=group_id,
                pair_idx=pair_idx,
                leg=leg,
                price=event_price,
                was_incomplete=True
            )

        # ANCESTOR BLOCK: Pairs from groups < current_group - 1 should NOT fire INIT
        if group_id < self.current_group - 1:
            print(f"[TP-INCOMPLETE-BLOCKED] Pair={pair_idx} Group={group_id} is ANCESTOR (< {self.current_group - 1}), ignoring INIT trigger")
            self._log_activity("TP-BLOCKED", f"{leg}{pair_idx} (Group {group_id}) hit TP @ {event_price:.2f} - ANCESTOR GROUP BLOCKED ({group_id} < {self.current_group - 1})")
        elif pair_idx in self._incomplete_pairs_init_triggered:
            print(f"[TP-INCOMPLETE-BLOCKED] Pair={pair_idx} already fired INIT before, skipping")
            self._log_activity("TP-BLOCKED", f"{leg}{pair_idx} hit TP @ {event_price:.2f} (INCOMPLETE) - DUPLICATE BLOCKED")
        elif self.graceful_stop:
            print(f"[TP-INCOMPLETE] Pair={pair_idx} Group={group_id} -> graceful stop active, no INIT")
            self._log_activity("TP", f"{leg}{pair_idx} hit TP @ {event_price:.2f} (INCOMPLETE) - GRACEFUL STOP")
        else:
            print(f"[TP-INCOMPLETE] Pair={pair_idx} Group={group_id} -> Firing INIT for Group {self.current_group + 1} (Bullish={is_bullish})")
            self._incomplete_pairs_init_triggered.add(pair_idx)

            # Log the activity
            self._log_activity("TP", f"{leg}{pair_idx} hit TP @ {event_price:.2f} (INCOMPLETE) -> INIT Group {self.current_group + 1}")

            # Pass triggering pair index so Init can fill the missing leg of previous group
            await self._execute_group_init(self.current_group + 1, event_price, is_bullish_source=is_bullish, trigger_pair_idx=pair_idx)
```

---

## Bug 6: Duplicate Activity Logs for Same TP Hit

### Problem (Bonus Fix)
Looking at the group logs, there are multiple duplicate TP entries for the same pair (e.g., S3 hit TP appears 3 times). This is likely because the TP detection fires multiple times before the position is cleaned up.

### Location
**Lines 3110-3118** in `_check_position_drops` (retirement logic logging)

### Fix
Add a check to prevent duplicate TP hit logging. Use a set to track logged TP hits.

Add to `__init__` (around line 450):
```python
# Track TP hits already logged to prevent duplicates
self._logged_tp_hits: Set[tuple] = set()  # (pair_idx, leg, group_id)
```

Then in the TP/SL logging block, add a duplicate check:
```python
if is_tp:
    # Prevent duplicate TP logging
    tp_key = (pair_idx, leg, group_id)
    if tp_key in self._logged_tp_hits:
        pass  # Skip duplicate
    else:
        self._logged_tp_hits.add(tp_key)
        self.group_logger.log_tp_hit(
            group_id=group_id,
            pair_idx=pair_idx,
            leg=leg,
            price=event_price,
            was_incomplete=was_incomplete
        )
```

---

## Summary Checklist

| # | Bug | File | Line | Fix |
|---|-----|------|------|-----|
| 1 | Live C instead of highwater | symbol_engine.py | 2981 | Use `_get_c_highwater()` + add C>=2 block for groups>0 |
| 2 | Non-atomic step for groups > 0 | symbol_engine.py | 1183, 1303 | Add C>=2 block for groups>0 |
| 3 | Non-atomic TP expand for groups > 0 | symbol_engine.py | 2759, 2838 | Add C>=2 block for groups>0 |
| 4 | Toggle trades don't update group_logger | symbol_engine.py | 3718 | Add `group_logger.update_pair()` |
| 5 | Group 0 incomplete TP not logged | symbol_engine.py | 3140 | Log incomplete TPs for all groups |
| 6 | Duplicate TP logs | symbol_engine.py | 3110, __init__ | Add dedup set |

---

## Expected Behavior After Fixes

1. **Groups > 0**: Natural expansion (STEP_EXPAND) stops at C_highwater >= 2
2. **Groups > 0**: TP-driven expansion stops at C_highwater >= 2
3. **Group 0 only**: Non-atomic expansion at C==2, followed by artificial TP -> INIT
4. **Toggle trades**: Entry prices shown correctly in group_logger table
5. **All groups**: Incomplete pair TP hits logged in activity
6. **All groups**: No duplicate TP hit entries in activity log

---

## Files to Modify

1. **core/engine/symbol_engine.py**
   - `_handle_completed_pair_expansion` (line 2981)
   - `_expand_bullish` (line 1183)
   - `_expand_bearish` (line 1303)
   - `_execute_tp_expansion` (lines 2759, 2838)
   - `_execute_trade_with_chain` (line 3718)
   - `_check_position_drops` (lines 3110, 3140)
   - `__init__` (add `_logged_tp_hits` set)
