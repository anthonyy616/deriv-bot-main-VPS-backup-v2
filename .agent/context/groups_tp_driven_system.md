# Groups + 3-CAP TP-Driven Trading System

## Overview

This document describes the TP-driven multi-group trading system implemented in `symbol_engine.py`. The system manages grid trading with atomic pair execution, dynamic grid expansion, and group rollover on Take Profit (TP) hits.

---

## Core Concepts

### Pairs
A **pair** consists of a BUY and SELL position at related price levels:
- **Pair N (positive)**: Buy at `anchor + N*D`, Sell at `anchor + (N-1)*D`
- **Pair 0**: Buy at `anchor`, Sell at `anchor - D`
- **Pair N (negative)**: Buy at `anchor + N*D`, Sell at `anchor + (N-1)*D`

Where `D` = spread (grid distance) and `anchor` = initial price.

### Pair States
- **Incomplete**: Only one leg filled (BUY xor SELL)
- **Complete**: Both legs filled (BUY and SELL)

### Completed Pairs Count (C)
`C` = number of pairs with BOTH positions currently open.

### Groups
Groups are isolated namespaces for pairs:
- **Group 0**: Pairs 0-99 (and negative pairs -1 to -99)
- **Group 1**: Pairs 100-199 (and -100 to -199)
- **Group 2**: Pairs 200-299, etc.

Each group maintains its own anchor price and pair indices.

---

## Trading Flow

### Phase 1: INIT
When bot starts or new group begins:
1. Place **B0** (Buy at anchor) - Pair 0 is incomplete
2. Place **S1** (Sell at anchor) - Pair 1 is incomplete
3. Both use **lot index 0** (smallest lot size)

**State after INIT:**
- Pair 0: B0 only (incomplete)
- Pair 1: S1 only (incomplete)
- C = 0

### Phase 2: Dynamic Grid Expansion
Grid expands atomically as price moves, until C >= 3:

**Price goes UP (bullish):**
1. At `anchor + 1*D`: Place **B1 + S2**
   - B1 completes Pair 1 (was: S1 only)
   - S2 starts Pair 2 (now: S2 only)
   - C = 1

2. At `anchor + 2*D`: Place **B2 + S3**
   - B2 completes Pair 2
   - S3 starts Pair 3
   - C = 2

3. At `anchor + 3*D`: Place **B3 + S4**
   - B3 completes Pair 3
   - S4 starts Pair 4
   - C = 3 → **STOP EXPANDING**

**Price goes DOWN (bearish):**
1. At `anchor - 1*D`: Place **S0 + B-1**
   - S0 completes Pair 0 (was: B0 only)
   - B-1 starts Pair -1 (now: B-1 only)
   - C = 1

2. At `anchor - 2*D`: Place **S-1 + B-2**
   - S-1 completes Pair -1
   - B-2 starts Pair -2
   - C = 2

3. At `anchor - 3*D`: Place **S-2 + B-3**
   - S-2 completes Pair -2
   - B-3 starts Pair -3
   - C = 3 → **STOP EXPANDING**

### Phase 3: Toggle Trading (C >= 3)
When C = 3:
- **No new pair legs created** (grid stops expanding)
- **Toggle trading continues** on completed pairs:
  - Each completed pair can trade: buy → sell → buy → sell...
  - Until max_positions reached → hedge executes
- **Incomplete pairs** remain: one on each end of the grid

### Phase 4: TP-Driven Group Rollover
When a Take Profit (TP) is hit:

**Incomplete Pair TP → New Group INIT:**
- When an incomplete pair's leg hits TP
- Create new group at TP price
- Execute INIT phase for new group (B100 + S101, etc.)

**Completed Pair TP → Group Expansion:**
- When a completed pair's leg hits TP
- Expand the current group (if C < 3)

---

## Example: Group 0 to Group 2 (Bullish Scenario)

### Group 0
```
INIT at anchor=100.00, D=20:
  B0 @ 100.00 (Pair 0 incomplete)
  S1 @ 100.00 (Pair 1 incomplete)
  C = 0

Price rises to 120:
  B1 + S2 → Pair 1 complete, Pair 2 incomplete
  C = 1

Price rises to 140:
  B2 + S3 → Pair 2 complete, Pair 3 incomplete
  C = 2

Price rises to 160:
  B3 + S4 → Pair 3 complete, Pair 4 incomplete
  C = 3 → LOCKED (no more expansion)

State:
  Incomplete: B0 (Pair 0), S4 (Pair 4)
  Complete: Pair 1, 2, 3 (toggle trading active)
```

### Group 0 → Group 1 Transition
```
B0's TP is at 120.00 (price rose past it)
B0 hits TP at 125.00

Since B0 is INCOMPLETE (Pair 0 has only B0, no S0):
  → CREATE GROUP 1 at anchor=125.00

Group 1 INIT:
  B100 @ 125.00 (Pair 100 incomplete)
  S101 @ 125.00 (Pair 101 incomplete)
  Lot index 0 for both
```

### Group 1 Expansion
```
Price continues bullish...

Completed pair TPs from Group 0 trigger expansion:
  When Pair 1 Buy TP hits → B101 + S102 (C_g1 = 1)
  When Pair 2 Buy TP hits → B102 + S103 (C_g1 = 2)
  When Pair 3 Buy TP hits → B103 + S104 (C_g1 = 3) → LOCKED
```

### Group 1 → Group 2 Transition
```
B100's TP at ~145.00 hits

Since B100 is INCOMPLETE:
  → CREATE GROUP 2 at TP price

Group 2 INIT:
  B200 @ 150.00
  S201 @ 150.00
  ...and pattern continues
```

---

## Current Bug: INIT Not Triggering on Incomplete Pair TP

### The Problem
When an incomplete pair's leg hits TP, the system should create a new group INIT. However, this is not happening.

### Root Cause: Ticket Map Mismatch

**When order is placed:**
```python
# After order_send() succeeds
positions = mt5.positions_get(symbol=self.symbol)
for pos in positions:
    if pos.magic == magic and pos.ticket not in self.ticket_map:
        position_ticket = pos.ticket  # e.g., 240200575
        break

ticket_map[240200575] = (cycle=0, pair=0, leg='B')
```

**When TP deal is detected:**
```python
deals = mt5.history_deals_get(from_time, to_time, symbol=symbol)
for deal in deals:
    if deal.reason == DEAL_REASON_TP:
        info = ticket_map.get(deal.position_id)  # e.g., looking for 240160469
        # Returns None because 240160469 != 240200575
```

**Evidence from logs:**
```
[TICKET_MAP] pos=240200575 -> (cycle=0, pair=0, leg=B)
...
[TP] Position 240160469 not in ticket_map, skipping cycle logic
```

### Why This Happens
MT5's `pos.ticket` (from open positions) does not match `deal.position_id` (from history deals) for some brokers. These are different internal identifiers.

### Impact
- TP deals are detected (`[DEBUG TP] Found X TP/SL deals...`)
- But lookup fails (`Position X not in ticket_map`)
- So `_execute_group_init()` never gets called
- New groups are never created

### Potential Fixes
1. **Use `result.order`**: Store the order ticket from `order_send()` result, which may match `deal.position_id`
2. **Comment parsing**: Parse the order comment (e.g., "B0 Grp0") to identify the pair
3. **Dual storage**: Store both `result.order` and `pos.ticket` as potential keys
4. **Magic number matching**: Use magic number + direction to identify the pair without ticket lookup

---

## Key Files and Methods

| File | Method | Purpose |
|------|--------|---------|
| `symbol_engine.py` | `_execute_group_init()` | Create B(offset) + S(offset+1) for new group |
| `symbol_engine.py` | `_check_step_triggers()` | Dynamic grid expansion until C >= 3 |
| `symbol_engine.py` | `_expand_bullish/bearish()` | Place atomic pair for expansion |
| `symbol_engine.py` | `_check_tp_sl_from_history()` | Detect TP/SL, trigger group rollover |
| `symbol_engine.py` | `_is_pair_incomplete()` | Check if pair has exactly one leg |
| `symbol_engine.py` | `_count_completed_pairs_open()` | Count pairs with both legs open (C) |

---

## Configuration

| Parameter | Description |
|-----------|-------------|
| `spread` | Grid distance (D) between levels |
| `tolerance` | Price tolerance for trigger detection |
| `max_positions` | Max trades per pair before hedge |
| `lot_sizes` | Array of lot sizes by trade index |
| `GROUP_OFFSET` | Pair index offset per group (default: 100) |
