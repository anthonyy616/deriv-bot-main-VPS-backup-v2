# Bug Fix Request: MT5 Grid Trading Bot

## Project Overview

This is a **Python trading bot** that implements a **"Ladder Grid" strategy** on MetaTrader 5 (MT5). The bot trades synthetic indices (FX Vol 20, 40, 60, 80) on the Deriv platform.

## Core Strategy Logic (MUST UNDERSTAND FULLY)

### Grid Structure
- The grid consists of **pairs** indexed as: ..., -2, -1, 0, +1, +2, ...
- **Pair 0** is the center, created at current market price when bot starts
- Each pair has a **BUY price** and **SELL price** separated by `spread` pips
- For positive pairs (idx > 0): SELL price < BUY price (sell below, buy above)
- For negative pairs (idx < 0): BUY price < SELL price (buy below, sell above)
- For Pair 0: BUY price > SELL price

### Trade Execution Rules (CRITICAL)
1. **Alternating Toggle**: Each pair MUST alternate between BUY and SELL
   - If `next_action = "buy"`, only BUY can execute
   - After BUY executes, toggle to `next_action = "sell"`
   - After SELL executes, toggle to `next_action = "buy"`

2. **Lot Sizing by trade_count**:
   - `lot_sizes = [0.1, 0.2, 0.3, 0.4, 0.5]` (configurable from UI)
   - First trade on pair uses `lot_sizes[0]` (0.1)
   - After each trade, `trade_count` increments
   - Next trade uses `lot_sizes[trade_count]`
   - Example: BUY@0.1 → SELL@0.2 → BUY@0.3 → SELL@0.4 → BUY@0.5

3. **max_positions per pair**:
   - `max_positions = 5` (configurable from UI)
   - Each pair can have at most 5 trades before it MUST wait for TP/SL reset
   - `trade_count < max_positions` must be enforced

4. **Chaining**: When B[n] executes, S[n+1] should chain-execute immediately (if at same price)
   - This is atomic - both trades happen together
   - Similarly, S[n] → B[n-1]

5. **Re-trigger**: When price leaves a level and returns, the pair should be able to trade again
   - Following the toggle (buy→sell→buy...)
   - Using the next lot size
   - Until max_positions is reached

### TP/SL Reset Rules (CRITICAL)
When **ANY** position on a pair hits TP or SL:
1. **NUCLEAR RESET** for that pair ONLY:
   - Close ALL positions for that pair (query MT5 by magic number)
   - Reset `trade_count = 0`
   - Reset `buy_filled = False`, `sell_filled = False`
   - Reset `next_action` based on pair index:
     - Pair <= 0: `next_action = "buy"`
     - Pair > 0: `next_action = "sell"`
   - Set `is_reopened = True` (bypass zone check for first trade)

2. After reset, the pair should be able to trade infinitely again (until next TP/SL)

### UI Configuration Fields
- `spread`: Distance between BUY and SELL prices (in pips)
- `lot_sizes`: Array like [0.1, 0.2, 0.3, 0.4, 0.5]
- `max_positions`: Max trades per pair before waiting for TP/SL (e.g., 5)
- `max_pairs`: Total number of pairs in grid (e.g., 5 means indices -2,-1,0,1,2)

---

## BUGS TO FIX

### Bug 1: Toggle Not Being Enforced - Same Direction Executes Twice
**Evidence:**
```
#016 | Pair  1 | SELL | Lot: 0.10 | #0 |
#017 | Pair  1 | SELL | Lot: 0.10 | #0 |  ← SELL executed TWICE at #0
```

**Root Cause:** After TP/SL reset, `is_reopened = True` bypasses zone check, but the trade executes multiple times before `is_reopened` is cleared, OR the `next_action` toggle is not advancing properly.

**Required Fix:** 
- The `next_action` toggle MUST advance atomically with trade execution
- A pair cannot execute the same direction twice in a row under ANY circumstance
- Add debug logging to verify toggle state before and after each trade

---

### Bug 2: Race Condition - Multiple Trades in Milliseconds
**Evidence:**
```
#020 [02:30:21.638] | Pair  1 | SELL | #0 |
#021 [02:30:21.763] | Pair  1 | BUY  | #1 |  ← 125ms later
#022 [02:30:21.895] | Pair  1 | SELL | #2 |  ← 132ms later
```

**Root Cause:** The mutex lock (`execution_lock`) is not preventing rapid-fire execution. Possibly:
- Lock is released, toggle advances, next tick triggers again immediately
- Zone transition check (`not pair.buy_in_zone`) is not working because `buy_in_zone` is set at end of loop

**Required Fix:**
- After a trade executes, the pair should NOT be able to trade again until:
  1. Price physically leaves the zone (zone transition detected)
  2. AND price returns to the zone
- The `buy_in_zone` / `sell_in_zone` flags must be set IMMEDIATELY after trade, not at end of loop

---

### Bug 3: trade_count Resets Too Frequently
**Evidence:**
```
Pair 0 executes BUY at #0 SIX times in the session
```

**Root Cause:** Every TP/SL hit on ANY position triggers a full reset. Even positions that were just opened get closed and reset.

**Required Fix:**
- Only reset when the ORIGINAL TP/SL (from first trade of cycle) is hit
- OR implement a minimum holding period
- OR track which positions belong to which "cycle" and only reset when a cycle's TP/SL is hit

---

### Bug 4: Cross-Symbol TP/SL Detection
**Evidence:**
Position ID 219304561 triggers `[TP_HIT]` on FX Vol 20, 40, 60, AND 80 simultaneously.

**Root Cause:** The `_check_tp_sl_from_history()` function queries deals but may not be filtering by symbol correctly. Or the magic number mapping is wrong.

**Required Fix:**
- Verify `mt5.history_deals_get()` is called with correct symbol filter
- Verify magic number calculation: `magic = 50000 + pair_idx` is symbol-specific
- Each symbol should ONLY process its own deals

---

### Bug 5: Terminate All Error
**Evidence:**
```
[ERROR] Failed to terminate FX Vol 60: 'int' object has no attribute 'type'
```

**Root Cause:** In the terminate function, a position ticket (int) is being accessed instead of the position object.

**Required Fix:**
- Find the terminate function and fix the iteration to properly access position objects, not ticket numbers

---

## File Location
Main strategy file: `core/strategy_engine.py`

## Key Methods to Examine
1. `_execute_trade_with_chain()` - Atomic trade execution with locking
2. `_check_virtual_triggers()` - Zone-based trigger logic
3. `_check_tp_sl_from_history()` - TP/SL detection and reset
4. `advance_toggle()` - Toggle next_action between buy/sell
5. `get_next_lot()` - Get lot size based on trade_count

## Expected Behavior After Fix
1. Each pair alternates strictly: BUY → SELL → BUY → SELL
2. Lot sizes increment: 0.1 → 0.2 → 0.3 → 0.4 → 0.5
3. Max 5 trades per pair before requiring TP/SL reset
4. After TP/SL reset, pair starts fresh with BUY@0.1 (for pair <= 0) or SELL@0.1 (for pair > 0)
5. No rapid-fire trades - must have zone exit/entry between trades
6. Each symbol only processes its own positions/deals

## Testing Verification
After fix, the trade log should show:
```
Pair 0: BUY @0.1 #0 → SELL @0.2 #1 → BUY @0.3 #2 → SELL @0.4 #3 → BUY @0.5 #4 → [TP/SL] → BUY @0.1 #0 → ...
```

Never:
```
Pair 0: BUY @0.1 #0 → BUY @0.1 #0 (same direction twice)
Pair 0: BUY @0.1 #0 → SELL @0.1 #0 (trade_count didn't increment)
```
