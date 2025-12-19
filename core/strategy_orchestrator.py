from typing import Dict, List, Set, Any
import asyncio
import time
from core.strategy_engine import GridStrategy

class StrategyOrchestrator:
    """
    Per-user orchestrator that manages multiple strategies (one per symbol).
    Works with the new multi-asset config structure.
    """
    
    def __init__(self, config_manager):
        self.config_manager = config_manager
        # Map symbol -> GridStrategy
        self.strategies: Dict[str, GridStrategy] = {}
        self.active_symbols: Set[str] = set()
        
        # Initialize
        self.update_strategies()

    @property
    def config(self):
        """Pass-through to config manager for the API"""
        return self.config_manager.get_config()

    def update_strategies(self):
        """
        Syncs active strategies with the configuration.
        Spawns new bots for enabled symbols, removes disabled ones.
        """
        # Get enabled symbols from new config structure
        enabled_symbols = set(self.config_manager.get_enabled_symbols())
        current_symbols = set(self.strategies.keys())

        # 1. Remove disabled symbols
        to_remove = current_symbols - enabled_symbols
        for sym in to_remove:
            print(f" Stopping Strategy: {sym}")
            del self.strategies[sym]

        # 2. Add newly enabled symbols
        to_add = enabled_symbols - current_symbols
        for sym in to_add:
            sym_config = self.config_manager.get_symbol_config(sym)
            if sym_config:
                print(f" Spawning Strategy: {sym}")
                strategy = GridStrategy(self.config_manager, sym)
                self.strategies[sym] = strategy

        self.active_symbols = enabled_symbols

    async def start(self):
        """Start all enabled strategies"""
        self.update_strategies()
        tasks = [bot.start() for bot in self.strategies.values()]
        if tasks:
            await asyncio.gather(*tasks)

    async def stop(self):
        """Stop all strategies"""
        tasks = [bot.stop() for bot in self.strategies.values()]
        if tasks:
            await asyncio.gather(*tasks)

    async def start_symbol(self, symbol: str):
        """Start a specific symbol strategy"""
        if symbol not in self.strategies:
            sym_config = self.config_manager.get_symbol_config(symbol)
            if sym_config and sym_config.get('enabled', False):
                print(f" Spawning Strategy: {symbol}")
                strategy = GridStrategy(self.config_manager, symbol)
                self.strategies[symbol] = strategy
                self.active_symbols.add(symbol)
        
        if symbol in self.strategies:
            await self.strategies[symbol].start()

    async def stop_symbol(self, symbol: str):
        """Stop a specific symbol strategy"""
        if symbol in self.strategies:
            await self.strategies[symbol].stop()
            del self.strategies[symbol]
            self.active_symbols.discard(symbol)

    async def start_ticker(self):
        """
        Called when config updates. Re-syncs strategies and notifies them.
        """
        self.update_strategies()
        tasks = [bot.start_ticker() for bot in self.strategies.values()]
        if tasks:
            await asyncio.gather(*tasks)

    async def on_external_tick(self, symbol, tick_data):
        """Routes the tick to the specific strategy for this symbol."""
        if symbol in self.strategies:
            await self.strategies[symbol].on_external_tick(tick_data)

    def get_active_symbols(self) -> List[str]:
        return list(self.active_symbols)

    def get_status(self) -> Dict[str, Any]:
        """
        Returns status for all active strategies.
        For multi-asset, returns per-symbol status in a 'strategies' dict.
        """
        if not self.strategies:
            return {
                "running": False,
                "current_price": 0,
                "open_positions": 0,
                "step": 0,
                "iteration": 0,
                "is_resetting": False,
                "strategies": {}
            }

        # Aggregate stats
        total_positions = 0
        running_any = False
        is_resetting_any = False
        per_symbol_status = {}
        
        for symbol, bot in self.strategies.items():
            s = bot.get_status()
            per_symbol_status[symbol] = s
            total_positions += s.get('open_positions', 0)
            if s.get('running', False):
                running_any = True
            if s.get('is_resetting', False):
                is_resetting_any = True
        
        # For backward compatibility, use first bot for single-value fields
        first_bot = list(self.strategies.values())[0] if self.strategies else None
        first_status = first_bot.get_status() if first_bot else {}

        return {
            "running": running_any,
            "current_price": first_bot.current_price if first_bot else 0,
            "open_positions": total_positions,
            "step": first_status.get('step', 0),
            "iteration": first_status.get('iteration', 0),
            "is_resetting": is_resetting_any,
            "active_count": len(self.strategies),
            "strategies": per_symbol_status
        }
