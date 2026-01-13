# core/persistence/repository.py
import aiosqlite
import logging
import time
from typing import Dict, List, Any
import os

# Ensure db directory exists
os.makedirs("db", exist_ok=True)
DB_PATH = "db/grid_v3.db"

class Repository:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.db = None

    async def initialize(self):
        """Connect and ensure schema exists."""
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row
        
        # Read schema file
        schema_path = os.path.join("db", "schema.sql")
        # Adjust path if running from root or core
        if not os.path.exists(schema_path):
             # Try absolute path based on project root assumption or relative
             current_dir = os.path.dirname(os.path.abspath(__file__))
             # core/persistence/ -> db/schema.sql? No, db is at root usually.
             # Assuming running from root:
             schema_path = "db/schema.sql"
        
        # Fallback to absolute path relative to this file if simple path fails
        if not os.path.exists(schema_path):
             # c:\...\core\persistence\..\..\db\schema.sql
             root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
             schema_path = os.path.join(root_dir, "db", "schema.sql")

        with open(schema_path, "r") as f:
            await self.db.executescript(f.read())
        await self.db.commit()

    async def get_state(self) -> Dict[str, Any]:
        """Load symbol-level state (phase, center_price)."""
        async with self.db.execute(
            "SELECT * FROM symbol_state WHERE symbol = ?", (self.symbol,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return {}

    async def save_state(self, phase: str, center_price: float, iteration: int):
        """Upsert symbol state."""
        await self.db.execute(
            """
            INSERT INTO symbol_state (symbol, phase, center_price, iteration, last_update_time)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                phase=excluded.phase,
                center_price=excluded.center_price,
                iteration=excluded.iteration,
                last_update_time=excluded.last_update_time
            """,
            (self.symbol, phase, center_price, iteration, time.time())
        )
        await self.db.commit()

    async def get_pairs(self) -> List[Dict[str, Any]]:
        """Load all active pairs for this symbol."""
        async with self.db.execute(
            "SELECT * FROM grid_pairs WHERE symbol = ?", (self.symbol,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def upsert_pair(self, pair_data: Dict[str, Any]):
        """Insert or Update a single pair (Atomic operation)."""
        # Extract fields from pair_data dict
        await self.db.execute(
            """
            INSERT INTO grid_pairs (
                symbol, pair_index, buy_price, sell_price, 
                buy_ticket, sell_ticket, buy_filled, sell_filled,
                buy_pending_ticket, sell_pending_ticket,
                trade_count, next_action, is_reopened,
                buy_in_zone, sell_in_zone,
                hedge_ticket, hedge_direction, hedge_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, pair_index) DO UPDATE SET
                buy_price=excluded.buy_price,
                sell_price=excluded.sell_price,
                buy_ticket=excluded.buy_ticket,
                sell_ticket=excluded.sell_ticket,
                buy_filled=excluded.buy_filled,
                sell_filled=excluded.sell_filled,
                buy_pending_ticket=excluded.buy_pending_ticket,
                sell_pending_ticket=excluded.sell_pending_ticket,
                trade_count=excluded.trade_count,
                next_action=excluded.next_action,
                is_reopened=excluded.is_reopened,
                buy_in_zone=excluded.buy_in_zone,
                sell_in_zone=excluded.sell_in_zone,
                hedge_ticket=excluded.hedge_ticket,
                hedge_direction=excluded.hedge_direction,
                hedge_active=excluded.hedge_active
            """,
            (
                self.symbol, pair_data['index'], pair_data['buy_price'], pair_data['sell_price'],
                pair_data.get('buy_ticket', 0), pair_data.get('sell_ticket', 0),
                pair_data.get('buy_filled', 0), pair_data.get('sell_filled', 0),
                pair_data.get('buy_pending_ticket', 0), pair_data.get('sell_pending_ticket', 0),
                pair_data.get('trade_count', 0), pair_data.get('next_action', 'buy'),
                pair_data.get('is_reopened', 0), pair_data.get('buy_in_zone', 0),
                pair_data.get('sell_in_zone', 0),
                pair_data.get('hedge_ticket', 0),
                pair_data.get('hedge_direction', None),
                pair_data.get('hedge_active', 0)
            )
        )
        await self.db.commit()

    async def delete_pair(self, pair_index: int):
        """Remove a pair (used in Leapfrog)."""
        await self.db.execute(
            "DELETE FROM grid_pairs WHERE symbol = ? AND pair_index = ?",
            (self.symbol, pair_index)
        )
        await self.db.commit()

    async def log_trade(self, event: Dict[str, Any]):
        """Log a trade event to history table (Permanent storage)."""
        await self.db.execute(
            """
            INSERT INTO trade_history (symbol, timestamp, event_type, pair_index, direction, price, lot_size, ticket, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.symbol, event['timestamp'], event['event_type'], 
                event['pair_index'], event['direction'], event['price'], 
                event['lot_size'], event['ticket'], event.get('notes', '')
            )
        )
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()
