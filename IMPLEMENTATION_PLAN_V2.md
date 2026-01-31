---

## Bug 7: `_create_next_negative_pair` Missing C Check and Wrong group_id

### Problem
When toggle trigger fires after non-atomic expansion (e.g., S0 re-entry), it calls `_create_next_negative_pair` to create B-1. This function:
1. Has NO C check - it creates pairs even when C >= 3 (or C >= 2 for groups > 0)
2. Sets `new_pair.group_id = self.current_group` instead of using the edge pair's group_id

This caused B-1 to be created when it shouldn't (after non-atomic S0), and B-1 got group_id=2 instead of group_id=0, leading to double INIT firing.

### Location
**Line 3453** in `_create_next_negative_pair`

### Current Code
```python
async def _create_next_negative_pair(self, edge_idx: int):
    """
    Create a new negative-index pair below the most-negative existing pair.
    """
    edge_pair = self.pairs.get(edge_idx)
    if edge_pair is None:
        print(f"[CREATE-NEG] Cannot find edge pair idx={edge_idx}")
        return

    # ... pair creation logic ...

    new_pair.group_id = self.current_group  # <-- BUG: Wrong group_id!
```

### Fixed Code
```python
async def _create_next_negative_pair(self, edge_idx: int):
    """
    Create a new negative-index pair below the most-negative existing pair.
    """
    edge_pair = self.pairs.get(edge_idx)
    if edge_pair is None:
        print(f"[CREATE-NEG] Cannot find edge pair idx={edge_idx}")
        return

    # FIX: Add C check to prevent creation after saturation
    group_id = edge_pair.group_id
    C = self._get_c_highwater(group_id)
    if C >= 3:
        print(f"[CREATE-NEG] BLOCKED: Group {group_id} C={C} >= 3 (saturated)")
        return
    if group_id > 0 and C >= 2:
        print(f"[CREATE-NEG] BLOCKED: Group {group_id} C={C} >= 2 (non-atomic only for Group 0)")
        return

    # ... pair creation logic ...

    new_pair.group_id = edge_pair.group_id  # <-- FIX: Use edge pair's group_id, not current_group
```

---

## Bug 8: `_create_next_positive_pair` Missing C Check and Wrong group_id

### Problem

Same issue as Bug 7 but for positive pair creation. When toggle trigger fires on the positive side, it can create pairs beyond saturation and with wrong group_id.

### Location

**Line 3370** in `_create_next_positive_pair`

### Current Code

```python
async def _create_next_positive_pair(self, edge_idx: int):
    """
    Create a new positive-index pair above the most-positive existing pair.
    """
    edge_pair = self.pairs.get(edge_idx)
    if edge_pair is None:
        print(f"[CREATE-POS] Cannot find edge pair idx={edge_idx}")
        return

    # ... pair creation logic ...

    new_pair.group_id = self.current_group  # <-- BUG: Wrong group_id!
```

### Fixed Code

```python
async def _create_next_positive_pair(self, edge_idx: int):
    """
    Create a new positive-index pair above the most-positive existing pair.
    """
    edge_pair = self.pairs.get(edge_idx)
    if edge_pair is None:
        print(f"[CREATE-POS] Cannot find edge pair idx={edge_idx}")
        return

    # FIX: Add C check to prevent creation after saturation
    group_id = edge_pair.group_id
    C = self._get_c_highwater(group_id)
    if C >= 3:
        print(f"[CREATE-POS] BLOCKED: Group {group_id} C={C} >= 3 (saturated)")
        return
    if group_id > 0 and C >= 2:
        print(f"[CREATE-POS] BLOCKED: Group {group_id} C={C} >= 2 (non-atomic only for Group 0)")
        return

    # ... pair creation logic ...

    new_pair.group_id = edge_pair.group_id  # <-- FIX: Use edge pair's group_id, not current_group
```

---

## Bug 9: `_get_group_from_pair` Returns None for Negative Indices

### Problem

When a pair with negative index doesn't exist in `self.pairs`, `_get_group_from_pair` returns `None` instead of 0. Negative indices (e.g., -1, -2) always belong to Group 0.

This can cause issues when looking up group_id for negative pairs that haven't been created yet.

### Location

**Line 708** in `_get_group_from_pair`

### Current Code

```python
def _get_group_from_pair(self, pair_idx: int) -> int:
    pair = self.pairs.get(pair_idx)
    if pair is not None:
        return pair.group_id
    if pair_idx >= 0:
        return pair_idx // self.GROUP_OFFSET
    # BUG: No return for negative indices when pair doesn't exist!
    # Returns None implicitly
```

### Fixed Code

```python
def _get_group_from_pair(self, pair_idx: int) -> int:
    pair = self.pairs.get(pair_idx)
    if pair is not None:
        return pair.group_id
    if pair_idx >= 0:
        return pair_idx // self.GROUP_OFFSET
    return 0  # FIX: Negative indices always belong to Group 0
```

---

## Bug 10: State Persistence & Restoration Failure

### Problem

The `load_state` method fails to restore two critical pieces of information from the database:

1. **Hedge State**: `hedge_active`, `hedge_ticket`, and `hedge_direction` are not read from the DB into the `GridPair` objects.
    * *Result*: On restart, the bot forgets it has active hedges. The `_enforce_hedge_invariants` check sees `trade_count >= max` but `hedge_active=False`, causing it to execute a **Duplicate Hedge**.
2. **Ticket Map**: `self.ticket_map` is not populated on restart.
    * *Result*: `_execute_hedge` relies on `ticket_map` to find the opposing position to inherit TP/SL from. If empty, **Hedge Inheritance Fails**, falling back to rough estimates.
    * *Result*: Interactive display (Grid Table) shows incorrect TP/SL/Open Status because it relies on `ticket_map`.

### Location

**Line 4913** (`load_state`) in `core/engine/symbol_engine.py`

### Fixed Code

```python
async def load_state(self):
    # ... (existing loading logic for symbol state) ...

    # [FIX] Restore Ticket Map First (Critical for logic that relies on lookup)
    try:
        self.ticket_map = await self.repository.get_ticket_map()
        print(f" {self.symbol}: Loaded {len(self.ticket_map)} tickets from DB")
    except Exception as e:
        print(f" {self.symbol}: Failed to load ticket map: {e}")
        self.ticket_map = {}

    # Load Pairs
    pair_rows = await self.repository.get_pairs()
    self.pairs = {}
    for row in pair_rows:
        # ... (existing pair reconstruction) ...

        # [FIX] Restore Hedge State
        pair.hedge_ticket = row.get('hedge_ticket', 0)
        pair.hedge_direction = row.get('hedge_direction')
        pair.hedge_active = bool(row.get('hedge_active', False))
        
        # ... (rest of logic) ...
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
| 6 | Duplicate TP logs | symbol_engine.py | 3110, **init** | Add dedup set |
| 7 | `_create_next_negative_pair` no C check + wrong group_id | symbol_engine.py | 3453 | Add C check + use `edge_pair.group_id` |
| 8 | `_create_next_positive_pair` no C check + wrong group_id | symbol_engine.py | 3370 | Add C check + use `edge_pair.group_id` |
| 9 | `_get_group_from_pair` returns None for negative indices | symbol_engine.py | 708 | Add `return 0` for negative indices |
| 10 | **Persistence Failure** (Hedges & Tickets lost) | symbol_engine.py | 4913 | Restore `ticket_map` and hedge fields in `load_state` |

## Critical Persistence Overhaul (Bugs 11-22)

The following bugs represent a systemic failure to persist state. They will be addressed by:

1. **Global State**: Serializing all missing global fields (`group_*` dicts, sets, flags) into the existing `symbol_state.metadata` JSON column.
2. **Pair State**: Adding a new `metadata` JSON column to `grid_pairs` to store complex pair data (`lot_history`, `timestamps`).

### Bug 11: `locked_entries` not restored

* **Fix**: Restore `locked_buy_entry` and `locked_sell_entry` in `load_state` (fields already exist in DB).

### Bug 12, 16, 17, 18: Global Group State Lost (`group_c_highwater`, `group_anchors`, etc.)

* **Fix**: Serialize `self.group_c_highwater`, `self.group_anchors`, `self.group_init_source`, `self.group_pending_retracement`, `self.current_group` into `symbol_state.metadata`.

### Bug 13, 14, 15: Global Sets Lost (`init_triggered`, `tp_expanded`, `logged_tp`)

* **Fix**: Serialize `_incomplete_pairs_init_triggered`, `_pairs_tp_expanded`, `_logged_tp_hits` (convert sets to lists) into `symbol_state.metadata`.

### Bug 19, 21: Global Logic State Lost (`ticket_touch_flags`, `step_triggers`)

* **Fix**: Serialize `ticket_touch_flags` and step trigger flags into `symbol_state.metadata`.

### Bug 20, 22: Complex Pair Data Lost (`lot_history`, `position_timestamps`)

* **Fix**:
  * **DB**: Add `metadata` TEXT column to `grid_pairs` via migration in `Repository.initialize`.
  * **Save**: Dump `buy_lot_history`, `sell_lot_history`, `position_timestamps` to JSON in `upsert_pair`.
  * **Load**: Parse JSON in `load_state` and populate pair objects.

---

## Files to Modify

1. **core/engine/symbol_engine.py**
    * `load_state`: Complete rewrite to restore all fields.
    * `save_state`: Serialize all global state to `metadata`.
    * `upsert_pair` call: Serialize pair state to `metadata`.
    * `__init__`: Ensure all sets/dicts are initialized if loading fails.

2. **core/persistence/repository.py**
    * `initialize`: Add migration for `grid_pairs.metadata` column.
    * `upsert_pair`: Handle `metadata` argument and SQL.

---
