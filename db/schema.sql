-- db/schema.sql
-- 1. Symbol State (Replaces parts of ladder_state_*.json)
CREATE TABLE IF NOT EXISTS symbol_state (
    symbol TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    center_price REAL,
    iteration INTEGER DEFAULT 1,
    last_update_time REAL,
    -- New: Cycle Management (Groups + 3-Cap Strategy)
    cycle_id INTEGER DEFAULT 0,
    anchor_price REAL DEFAULT 0.0
);
-- 2. Grid Pairs (Replaces 'pairs' dict in JSON)
CREATE TABLE IF NOT EXISTS grid_pairs (
    symbol TEXT NOT NULL,
    pair_index INTEGER NOT NULL,
    -- Price Levels
    buy_price REAL NOT NULL,
    sell_price REAL NOT NULL,
    -- Execution State
    buy_ticket INTEGER DEFAULT 0,
    sell_ticket INTEGER DEFAULT 0,
    buy_filled BOOLEAN DEFAULT 0,
    sell_filled BOOLEAN DEFAULT 0,
    buy_pending_ticket INTEGER DEFAULT 0,
    sell_pending_ticket INTEGER DEFAULT 0,
    -- The "Brain" (Logic Memory)
    trade_count INTEGER DEFAULT 0,
    next_action TEXT DEFAULT 'buy',
    -- 'buy' or 'sell'
    is_reopened BOOLEAN DEFAULT 0,
    -- Zone Flags
    buy_in_zone BOOLEAN DEFAULT 0,
    sell_in_zone BOOLEAN DEFAULT 0,
    -- Hedge System (Section 9)
    hedge_ticket INTEGER DEFAULT 0,
    hedge_direction TEXT,
    -- 'buy' or 'sell'
    hedge_active BOOLEAN DEFAULT 0,
    -- LOCKED ENTRY PRICES: Set once on first execution, never changed
    -- Ensures re-entries happen at exact same price level
    locked_buy_entry REAL DEFAULT 0.0,
    locked_sell_entry REAL DEFAULT 0.0,
    PRIMARY KEY (symbol, pair_index)
);
-- 3. Trade History (Replaces in-memory self.trade_history list)
CREATE TABLE IF NOT EXISTS trade_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    -- OPEN, TP_HIT, SL_HIT, etc.
    pair_index INTEGER,
    direction TEXT,
    price REAL,
    lot_size REAL,
    ticket INTEGER,
    notes TEXT
);
-- 4. Ticket Map (Groups + 3-Cap Strategy: tracks ticket → pair/leg/prices)
-- Extended to store TP/SL levels for deterministic TP/SL detection
CREATE TABLE IF NOT EXISTS ticket_map (
    ticket INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    cycle_id INTEGER NOT NULL,
    pair_index INTEGER NOT NULL,
    leg TEXT NOT NULL CHECK (leg IN ('B', 'S')),
    trade_count INTEGER DEFAULT 0,
    entry_price REAL DEFAULT 0.0,
    tp_price REAL DEFAULT 0.0,
    sl_price REAL DEFAULT 0.0
);
-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_pairs_symbol ON grid_pairs(symbol);
CREATE INDEX IF NOT EXISTS idx_history_symbol ON trade_history(symbol);
CREATE INDEX IF NOT EXISTS idx_ticket_map_symbol ON ticket_map(symbol);
CREATE INDEX IF NOT EXISTS idx_ticket_map_cycle ON ticket_map(cycle_id);