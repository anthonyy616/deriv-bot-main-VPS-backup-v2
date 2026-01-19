# Fixes and Problems Log

## Session Date: 2026-01-19

---

## Fixes Implemented

### 1. Group 0 INIT Logic Fix

**Problem:** INIT was being queued (waiting for C==3) even for Group 0 when an incomplete pair hit TP.
**Fix:** Group 0 now fires INIT immediately when incomplete pair TP hits. C==3 gating only applies to Groups 1+.
**Location:** `_check_position_drops()` lines ~2300-2315

### 2. Phoenix System Removal

**Problem:** Phoenix reset logic was recycling pairs after TP/SL, which is no longer needed with the group system.
**Fix:** Removed `_phoenix_reset_pair()` function and all calls to it. Also removed proximity-based re-entry for phoenix pairs.
**Location:** Multiple locations throughout `symbol_engine.py`

### 3. TP/SL Alignment/Inheritance Removal

**Problem:** TP/SL values were being inherited between legs (e.g., B0's TP became S1's SL), causing inconsistent stop levels.
**Fix:** Removed the alignment logic. Each leg now calculates its own independent TP/SL.
**Location:** `_execute_market_order()` lines ~3750-3765

### 4. TP/SL Base Price Fix

**Problem:** TP/SL was calculated from `grid_price` (theoretical level) instead of `exec_price` (actual entry). This caused TP/SL distance to be incorrect when there was slippage.
**Example:**

- grid_price = 116776, exec_price = 116739 (37 pips difference)
- TP calculated as grid_price - 90 = 116686
- Actual distance from exec was only 53 pips, not 90!
**Fix:** Changed TP/SL calculation to use `exec_price` instead of `grid_price`.
**Location:** `_execute_market_order()` lines ~3751-3765

### 5. Nuclear Reset Removal (Survivor Leg Preservation)

**Problem:** When one leg of a completed pair closed (TP or SL), the system was closing the survivor leg (nuclear reset).
**Fix:** Commented out all survivor leg closing logic. When one leg closes, the other stays open and the pair is still counted as completed.
**Locations:**

- `_check_position_drops()` - survivor cleanup commented out
- `_check_tp_sl_from_history()` - survivor cleanup commented out
- `_execute_pair_reset()` - both duplicate functions commented out
- Calls to `_execute_pair_reset()` - commented out

---

## Current Problems

### Problem 1: TP Not Triggering Expansion for Groups 1+

**Description:** For groups past Group 0 (i.e., Group 1, Group 2, etc.), when a COMPLETED pair hits TP, the expansion is not being triggered properly.

**Evidence from logs:**

```
[DROP-COMPLETE] pair=100 leg=S → Checking expansion
```

- Pair 100 (Group 1) was a completed pair
- Sell leg hit TP
- Log shows "Checking expansion" but no expansion occurred
- Expected: Should trigger bullish or bearish expansion based on which leg hit TP

**Likely Cause:**
The `_handle_completed_pair_expansion()` method is only called when `self.current_group > 0`, but there may be an issue with:

1. How the expansion is being triggered for Group 1+ pairs
2. The CMP (Current Market Price) being passed
3. The logic not properly identifying edge pairs in the new group's pair index range (100-199 for Group 1)

**Expected Behavior:**
When a completed pair in Group 1 (pair 100) has its sell leg hit TP:

1. Should detect this as a completed pair TP
2. Should call expansion logic for Group 1
3. Should seed new pairs based on the expansion direction

**Code Investigation Needed:**

- `_handle_completed_pair_expansion()` - check if it's properly finding edge pairs in current group
- `_check_position_drops()` - verify the expansion call is being reached
- Check if `current_group` is correctly set to 1 after Group 1 INIT

---

## Investigation (2026-01-19 Session)

### Additional Findings

**Issue in `_check_position_drops()` Line 2283:**

```python
was_incomplete = not (pair.buy_filled and pair.sell_filled)
```

**Problem:** The `was_incomplete` check uses the CURRENT state of `pair.buy_filled` / `pair.sell_filled` AFTER the position has already dropped. This may give stale results because:

- If leg B just closed, `pair.buy_filled` might still be `True` (not yet updated)
- Or it might already be stale from a previous cycle

**Potential Root Cause for Expansion Not Firing:**
In `_handle_completed_pair_expansion()`, edge pairs are found by looking for:

- **Bullish edge:** `sell_filled=True` AND `buy_filled=False`
- **Bearish edge:** `buy_filled=True` AND `sell_filled=False`

If no pairs match these criteria in the current group, NO expansion occurs. This could happen if:

1. Group 1 was never properly initialized (no pairs 100-199 exist)
2. Pair fill states are inconsistent with actual MT5 positions
3. `current_group` is still 0 when it should be 1

### Logging Added

Added `[TP-LOG]` and `[COMP-EXPAND]` debug output to track:

- Pair status (complete/incomplete) on every position drop
- Edge pair discovery results in expansion check
- CMP vs price level comparisons

---

## Fix Implemented (2026-01-19 Session)

### Group Tracking Per Pair

**Root Cause Identified:**
When Group 1 expands bearish from pair 100, it creates B99. But `_get_group_from_pair()` used integer division: `99 // 100 = 0`, so B99 was categorized as Group 0!

**Fix Applied:**

1. Added `group_id: int = 0` field to `GridPair` dataclass
2. Updated `_get_group_from_pair()` to use stored `pair.group_id` when pair exists (fallback to calculation for legacy)
3. Updated ALL GridPair creations to set `group_id = self.current_group` (or explicit group_id)

**Affected locations:**

- `_execute_group_init()` - B(offset) and S(offset+1)
- `_handle_completed_pair_expansion()` - seed pairs
- `_expand_bullish()` / `_expand_bearish()` - new pairs
- `_place_atomic_bullish_tp()` / `_place_atomic_bearish_tp()` - TP expansion pairs
- `_place_single_leg_tp()` - single leg expansion
- Step functions (`_execute_step1_bullish`, etc.) - legacy pairs
- Init phase (`_process_init_phase()`) - pair0 and pair1

**Result:**
Now when expansion creates B99 for Group 1, it's explicitly tagged with `group_id=1`, so subsequent expansion checks correctly find it in Group 1.

---

### Expansion Group Filtering Fix (2026-01-19 Session 2)

**Root Causes Identified:**

1. **B102 Skipped:** `_check_step_triggers()` used `anchor_price` (Group 0's anchor) to calculate trigger levels for ALL pairs. For Group 1 pairs like 102, `anchor_price + 102 * spread` gives wrong trigger levels.

2. **Group 0 Pairs Continued After Group 1:** `_check_step_triggers()` had NO group filtering - it processed ALL pairs in `self.pairs`, including Group 0 pairs after Group 1 started.

**Fix Applied:**

1. **`_check_step_triggers()`** - Complete rewrite:
   - Filter pairs by `pair.group_id == self.current_group`
   - Use `_count_completed_pairs_for_group(self.current_group)` instead of global C
   - Use stored `pair.buy_price` / `pair.sell_price` for trigger levels instead of calculating from anchor

2. **`_expand_bullish()` / `_expand_bearish()`**:
   - Changed C counting to use `_count_completed_pairs_for_group(self.current_group)`
   - Derive new pair prices from completing pair (`pair.buy_price` / `pair.sell_price`) instead of global `anchor_price`

**Result:**

- Group 0 expansion stops when Group 1 starts (no more trades for -2, -3, etc.)
- Group 1 expansion correctly places B102 before S103 (atomic pairs preserved)

---

### B102 Failure and Nuclear Reset Fix (2026-01-19 Session 3)

**Root Causes Identified:**

1. **B102 Blocked By Global CAP:** `_can_place_completing_leg()` used `_count_completed_pairs_open()` which counts C **globally across ALL groups**. When Group 0 had 3 completed pairs, B102 was blocked even though Group 1's C was only 1.

2. **Nuclear Reset Needed:** User requested that when a completed pair hits TP, the survivor leg should be closed (nuclear reset). On SL, survivor stays open.

**Fixes Applied:**

1. **`_can_place_completing_leg()`**:
   - Changed to use `_count_completed_pairs_for_group(pair.group_id)` instead of global count
   - Now B102 will not be blocked by Group 0's C=3

2. **Nuclear Reset on Completed Pair TP**:
   - Added TP vs SL detection for completed pairs in `_check_position_drops()`
   - On TP: Close survivor leg (nuclear reset)
   - On SL: Keep survivor open

**Result:**

- Group 1+ pairs can now expand correctly even when Group 0 has 3 completed pairs
- Completed pairs hitting TP will close the survivor leg
- Completed pairs hitting SL will keep the survivor open

---

### Non-Atomic Expansion Race Fix (2026-01-19 Session 4)

**Root Cause Identified:**
Atomic expansion (B102 + S103) and TP-driven expansion (S2 TP → B103) were racing.

1. Atomic expansion places B102 + S103 (bumping real C to 3).
2. TP-driven `COMP_EXPAND` (triggered by previous S2 TP) runs concurrently with stale C=2.
3. Finds pair 103 as edge, sees C=2, executes non-atomic expansion (B103 only).
4. Result: Double expansion (S103 then immediately B103).

**Fix Applied:**
Added a **C re-check** inside the non-atomic block of `_handle_completed_pair_expansion`:

```python
if C == 2:
    current_C = self._count_completed_pairs_for_group(group_id)
    if current_C >= 3:
        return  # ABORT: Race detected!
    # proceed with non-atomic expansion...
```

**Result:**
If atomic expansion bumps C to 3 during the race, the non-atomic expansion will detect it and abort, preventing B103 from firing immediately.
