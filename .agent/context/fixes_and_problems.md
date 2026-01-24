# Fixes and Problems Log

## Session Date: 2026-01-23

---

## Fixes Implemented This Session

### 1. Normal Expansion for All Groups (Infinite Groups Fix)

**Problem:** Step trigger expansion was only working for Group 0. Groups 1+ were waiting for completed pair TPs to drive expansion, which was incorrect. The strategy requires ALL groups to expand normally via step triggers.

**Fix:** Removed the guard that blocked step triggers for Group 1+.

1. Removed `if self.current_group > 0: return` from `_check_step_triggers()`.
2. All groups now expand normally: INIT → atomic expansion until C=2 → non-atomic at C=3 → artificial TP → INIT next group.
3. Each group uses its own anchor price stored in `group_anchors`.

**Example Flow:**

- Group 0: Price 0→50→100→150 (bullish) → C=3 → INIT Group 1 at 150
- Group 1: Price 150→100→50→0 (bearish) → C=3 → INIT Group 2 at 0
- Group 2: Price 0→50→100→150 (bullish) → C=3 → INIT Group 3 at 150
- ... pattern repeats infinitely

**Location:** `symbol_engine.py` (lines ~811-823)

---

### 2. Incomplete Pair TP → INIT for Next Group

**Problem:** Natural incomplete pair TPs were only doing cleanup, not firing INIT. This prevented infinite group progression in some scenarios.

**Fix:** Incomplete pair TPs now fire INIT for the next group.

1. Added `_incomplete_pairs_init_triggered: Set[int]` to prevent duplicate INITs from the same pair.
2. When an incomplete pair hits TP, INIT fires for `current_group + 1` at the TP event price.
3. Subsequent TP hits on that same pair are blocked (set protection).

**Location:** `symbol_engine.py` (lines ~440, ~2298-2307)

---

### 3. Completed Pair TP Skips Expansion When Normal Expansion Active

**Problem:** Completed pair TPs could fire TP-driven expansion even when step triggers were already handling expansion (C < 3). This could cause double expansion.

**Fix:** Added C < 3 check to skip TP-driven expansion when normal expansion is active.

1. If `C < 3` for current group, completed pair TP logs `[TP-COMPLETE-SKIP]` and does not fire expansion.
2. TP-driven expansion only fires when `C >= 3` (group is locked).

**Location:** `symbol_engine.py` (lines ~2308-2316)

---

### 4. Permanent Per-Pair TP Expansion Lock

**Problem:** The TP expansion logic used a time-based debounce (5 seconds), which allowed the same pair to trigger expansion multiple times if TP events occurred spaced out (e.g., B102 + S103 firing twice). This caused grid inconsistency and unintended trade sequences.

**Fix:** Replaced the global time-based debounce with a permanent per-pair lock.

1. Removed `_expansion_debounce_seconds` and `_pair_last_expansion_time`.
2. Implemented `_pairs_tp_expanded: Set[int]` to track pairs that have fired expansion.
3. Once a completed pair hits TP and fires expansion, it is permanently added to the set and cannot fire expansion again for the rest of the session.

**Location:** `symbol_engine.py` (lines ~430, ~2300)

---

## Session Date: 2026-01-22

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

# Fixes and Problems Log

## Session Date: 2026-01-21

---

## Fixes Implemented This Session

### 1. Deterministic TP/SL Touch Flags

**Problem:** TP/SL classification was unreliable - sometimes positions would close but the system couldn't determine if it was TP or SL.

**Fix:** Implemented latching touch flags that are updated on every tick:

```python
def _update_tp_sl_touch_flags(self, ask: float, bid: float):
    for ticket, info in self.ticket_map.items():
        pair_idx, leg, _, tp_price, sl_price = info  # 5-tuple unpack
        if leg == 'B':
            if bid >= tp_price: flags['tp_touched'] = True
            if bid <= sl_price: flags['sl_touched'] = True
        else:  # SELL
            if ask <= tp_price: flags['tp_touched'] = True
            if ask >= sl_price: flags['sl_touched'] = True
```

**Location:** `_update_tp_sl_touch_flags()` called first in `_handle_running()`

---

### 2. MT5-Authoritative C Counting

**Problem:** `_count_completed_pairs_for_group()` used in-memory `pair.buy_filled/sell_filled` which could be stale.

**Fix:** Rewrote to use live MT5 positions + ticket_map:

```python
def _count_completed_pairs_for_group(self, group_id: int) -> int:
    positions = mt5.positions_get(symbol=self.symbol)
    pair_legs = defaultdict(set)
    for pos in positions:
        info = self.ticket_map.get(pos.ticket)
        if info and len(info) >= 5:
            pair_idx, leg, _, _, _ = info
            pair_legs[pair_idx].add(leg)
    # Count pairs with both legs in specified group
    count = sum(1 for p_idx, legs in pair_legs.items()
                if legs == {'B', 'S'} and self._get_group_from_pair(p_idx) == group_id)
    return count
```

**Location:** `_count_completed_pairs_for_group()` and `_count_completed_pairs_open()`

---

### 3. Artificial TP Concept (Immediate Close + INIT)

**Problem:** Bot was not transitioning to Group 1+. The system waited for incomplete pairs to naturally hit TP, which:

- Could hit on the wrong edge (bearish incomplete when bullish expansion happened)
- Could take forever if price moved away
- Was unnecessarily complex

**Fix:** When C==2 non-atomic fires (making C=3), **immediately**:

1. Close the incomplete pair position
2. Fire INIT for next group

**Implementation:**

```python
# In _expand_bullish() and _expand_bearish():
if C == 2:
    event_price = pair.buy_price  # or sell_price for bearish
    print(f"[NON-ATOMIC] C was 2, now 3 -> forcing artificial TP + INIT")
    await self._force_artificial_tp_and_init(tick, event_price=event_price)
    return
```

**Location:**

- `_expand_bullish()` C==2 branch
- `_expand_bearish()` C==2 branch
- `_execute_tp_expansion()` C==2 branches

---

### 4. Completed/Incomplete Uses In-Memory State (Not MT5)

**Problem:** When determining if a dropped position was from a completed or incomplete pair, the system checked MT5 positions for the other leg. But if one leg already closed via SL, the pair appeared "incomplete" even though it was completed.

**Example:**

1. Pair 101 had both B101 and S101 (completed)
2. B101 hit SL first → closed
3. S101 hit TP later → system saw only 1 leg in MT5 → marked "Incomplete"
4. Routed as incomplete TP → no expansion fired

**Fix:** Use in-memory pair state which remembers "ever filled":

```python
# In _check_position_drops():
pair = self.pairs.get(pair_idx)
was_completed = pair and pair.buy_filled and pair.sell_filled
was_incomplete = not was_completed
```

**Key Insight:** `pair.buy_filled` and `pair.sell_filled` don't get reset when positions close. They remember the pair was once completed.

**Location:** `_check_position_drops()` lines 2510-2514

---

### 5. Removed Pending Rollover Gate

**Problem:** Earlier fix tried to use a `pending_group_rollover` gate to control when incomplete TPs could fire INIT. This was overly complex and failed when:

- The incomplete TP hit on the wrong edge
- Multiple incomplete pairs existed

**Fix:** Removed the gate entirely. INIT is now ONLY triggered by the artificial TP mechanism (immediate close when C==2 non-atomic fires), never by natural incomplete TPs.

**Location:** Removed `pending_group_rollover` flag and all related checks

---

### 6. Ticket Map 5-Tuple Validation

**Problem:** Various methods had inconsistent tuple unpacking for ticket_map entries.

**Fix:** Enforced canonical 5-tuple everywhere:

```python
# Canonical format:
ticket_map[ticket] = (pair_idx, leg, entry_price, tp_price, sl_price)

# Correct unpack:
pair_idx, leg, entry_price, tp_price, sl_price = info

# Or when only needing first elements:
pair_idx, leg, _, _, _ = info
```

**Locations:** All methods that access ticket_map

---

## Previous Session Fixes (2026-01-19)

### Group Tracking Per Pair

- Added `group_id` field to GridPair dataclass
- Updated `_get_group_from_pair()` to use stored group_id

### Expansion Group Filtering

- `_check_step_triggers()` filters by current_group
- Uses stored `pair.buy_price/sell_price` for trigger levels

### Group-Scoped C Gating

- `_can_place_completing_leg()` uses group-specific C count

### Non-Atomic Race Fix

- C re-check inside non-atomic block prevents double expansion

---

## Architecture Summary

### Tick Loop Order (Critical)

```
_handle_running():
    1. _update_tp_sl_touch_flags(ask, bid)  # FIRST: latch crossings
    2. _check_position_drops(ask, bid)       # SECOND: detect drops
    3. _check_step_triggers(ask, bid)        # Expansion triggers
    4. _enforce_hedge_invariants_gated()     # Hedge logic
    5. _check_virtual_triggers(ask, bid)     # Toggle trading
```

### Position Drop Flow

```
Position closes in MT5
    ↓
_check_position_drops() detects missing ticket
    ↓
Read touch flags: tp_touched / sl_touched
    ↓
Determine was_completed from pair.buy_filled && pair.sell_filled
    ↓
Route:
  - Completed pair TP → _execute_tp_expansion()
  - Completed pair SL → cleanup only
  - Incomplete pair TP/SL → cleanup only (no INIT here)
```

### Expansion Flow

```
Step trigger or TP drives expansion
    ↓
Check C for current group (MT5-authoritative)
    ↓
C < 2: Atomic expansion (B + S)
C == 2: Non-atomic (completing leg only)
    ↓
If C == 2: _force_artificial_tp_and_init()
    ↓
1. Find incomplete pair in current group
2. Close it
3. Fire INIT for next group
```

---

## Invariants (Must Always Hold)

1. **Ticket map is 5-tuple:** `(pair_idx, leg, entry_price, tp_price, sl_price)`

2. **C counting is MT5-authoritative:** Never use `pair.buy_filled/sell_filled` for C

3. **Completed determination is in-memory:** Use `pair.buy_filled and pair.sell_filled` (remembers "ever filled")

4. **INIT only via artificial TP:** Never trigger INIT from natural incomplete TP drops

5. **Touch flags before drops:** `_update_tp_sl_touch_flags()` must run before `_check_position_drops()`

6. **Group-scoped C:** All C checks use `_count_completed_pairs_for_group(group_id)`

7. **LOCKED ENTRY PRICES IMMUTABLE:** Once set, `locked_buy_entry` and `locked_sell_entry` never change

---

## Session Date: 2026-01-22

---

## Fixes Implemented This Session

### 1. Locked Entry Prices (Grid Drift Fix)

**Problem:** Re-entries were happening at WRONG price levels because:

1. When a position left the grid (TP/SL), the original entry price was lost
2. `_check_virtual_triggers` recalculated trigger levels using dynamic formula:

   ```python
   # OLD (WRONG): Used relative calculation
   buy_trigger = pair.sell_price + spread  # Dynamic!
   ```

3. This caused S100 to re-enter at 119749 when original was 119683 (66 pips drift!)

**Example from logs:**

- Trade #18: S100 opened at **119683.53** (original)
- Trade #27: S100 re-opened at **119749.56** (WRONG - 66 pips higher!)

**Fix Applied:**

1. Added `locked_buy_entry` and `locked_sell_entry` to `GridPair` dataclass
2. In `_execute_market_order()`: Set locked entry when trade first executes

   ```python
   if pair.locked_buy_entry == 0.0:
       pair.locked_buy_entry = exec_price
       print(f"[LOCKED] Pair {index} BUY entry locked at {exec_price:.2f}")
   ```

3. In `_check_virtual_triggers()`: Use locked entries instead of recalculating

   ```python
   buy_trigger = pair.locked_buy_entry if pair.locked_buy_entry > 0 else pair.buy_price
   sell_trigger = pair.locked_sell_entry if pair.locked_sell_entry > 0 else pair.sell_price
   ```

4. Updated `schema.sql` and `repository.py` for persistence

**Locations:**

- `GridPair` class (lines 93-98)
- `_execute_market_order()` (lines 3833-3845)
- `_check_virtual_triggers()` (lines 3346-3362, 3412-3423)
- `db/schema.sql` (grid_pairs table)
- `core/persistence/repository.py` (upsert_pair method)

**Result:**

- Once B100 enters at 119783, that's B100's level FOREVER
- Once S100 enters at 119683, that's S100's level FOREVER
- Re-entries happen at exact same price levels
- Grid structure remains stable across the session

---

## Invariant Added

1. **LOCKED ENTRY PRICES IMMUTABLE:** Once `locked_buy_entry` or `locked_sell_entry` is set (non-zero), it NEVER changes. Re-entries must use these exact prices.

---

## Session Date: 2026-01-25

---

## Fixes Implemented This Session

### 1. Unified Directional Guards (Global Application)

**Problem:** Directional guards (blocking expansion in the direction of the initial trend) were only applied if `self.current_group == 1`. The user requested this logic to apply to ALL groups to ensure consistent behavior across the entire grid lifecycle.

**Fix:** Removed the `current_group == 1` condition from all guard checks and unified the bias capture.

1. **Renamed Attribute**: `self.group_1_direction` was renamed to `self.group_direction` to reflect its global scope.
2. **Global Checks**: Removed `if self.current_group == 1` from:
    - `_check_step_triggers()` (Bullish/Bearish guards)
    - `_expand_bullish()` / `_expand_bearish()`
    - `_create_next_positive_pair()` / `_create_next_negative_pair()`
3. **Capture Logic**: Initial direction (BULLISH/BEARISH) is still captured during Group 1 initialization but now restricts expansion for all subsequent groups.

**Locations:** `symbol_engine.py` (lines ~415, ~768, ~928, ~966, ~988, ~1056, ~2656, ~2731)

---

### 2. State Persistence Fix (Syntax/Metadata)

**Problem:** Manual code edits introduced broken logic in `save_state` and `load_state`.

- `save_state`: Metadata dictionary had unquoted keys and invalid syntax (e.g., `": self`).
- `load_state`: Attempted to assign to `self` directly (e.g., `self = metadata.get('...)`).

**Fix:** Corrected dictionary serialization and variable assignments.

- Restored `"group_direction": self.group_direction` in `metadata_dict`.
- Fixed `load_state` to assign to `self.group_direction`.

**Locations:** `symbol_engine.py` (lines ~4247-4283)

---

### 3. Startup and Indentation Fix

**Problem:** A stray `1` character was inserted during manual editing, causing an `IndentationError` and preventing the bot from starting.

**Fix:** Removed the stray character and corrected indentation block for legacy fields.

**Location:** `symbol_engine.py` (line ~411)
