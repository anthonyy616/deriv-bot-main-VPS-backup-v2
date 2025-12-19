"""
Trading Engine with MT5 Health Monitoring

Production-grade engine with:
1. Auto-reconnect on MT5 disconnection
2. Health monitoring every N ticks
3. Graceful error handling
4. Detailed logging for debugging
"""

import asyncio
import MetaTrader5 as mt5
import os
import logging
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

logger = logging.getLogger("engine")


class TradingEngine:
    """
    High-performance trading engine with MT5 health monitoring.
    """
    
    # Health check interval (every N ticks)
    HEALTH_CHECK_INTERVAL = 100
    # Max reconnection attempts before raising
    MAX_RECONNECT_ATTEMPTS = 10
    # Delay between reconnection attempts (seconds)
    RECONNECT_DELAY = 5
    
    def __init__(self, bot_manager):
        self.bot_manager = bot_manager
        self.running = True
        self.tick_count = 0
        self.last_health_check = datetime.now()
        self.consecutive_errors = 0
        
        # MT5 Configuration
        self.login = int(os.getenv("MT5_LOGIN", 0))
        self.password = os.getenv("MT5_PASSWORD", "")
        self.server = os.getenv("MT5_SERVER", "")
        self.path = os.getenv("MT5_PATH", "")
        
        # Stats for monitoring
        self.stats = {
            "ticks_processed": 0,
            "reconnects": 0,
            "errors": 0,
            "last_tick_time": None
        }

    def _init_mt5(self) -> bool:
        """
        Initialize MT5 connection with error handling.
        Returns True if successful.
        """
        try:
            # Shutdown any existing connection first
            mt5.shutdown()
            
            # Initialize
            if not mt5.initialize(path=self.path if self.path else None):
                error = mt5.last_error()
                logger.error(f"MT5 initialize failed: {error}")
                return False
            
            # Login
            if not mt5.login(self.login, password=self.password, server=self.server):
                error = mt5.last_error()
                logger.error(f"MT5 login failed: {error}")
                mt5.shutdown()
                return False
            
            logger.info("[OK] MT5 connected successfully")
            return True
            
        except Exception as e:
            logger.error(f"MT5 init exception: {e}")
            return False

    async def _reconnect_mt5(self) -> bool:
        """
        Attempt to reconnect to MT5 with retry logic.
        Returns True if reconnection successful.
        """
        logger.warning("[WARN] MT5 connection lost. Attempting reconnection...")
        
        for attempt in range(1, self.MAX_RECONNECT_ATTEMPTS + 1):
            logger.info(f"Reconnection attempt {attempt}/{self.MAX_RECONNECT_ATTEMPTS}...")
            
            if self._init_mt5():
                self.stats["reconnects"] += 1
                logger.info(f"[OK] MT5 reconnected on attempt {attempt}")
                return True
            
            await asyncio.sleep(self.RECONNECT_DELAY)
        
        logger.critical(f"Failed to reconnect after {self.MAX_RECONNECT_ATTEMPTS} attempts")
        return False

    def _check_mt5_health(self) -> bool:
        """
        Check if MT5 is still connected and responsive.
        Returns True if healthy.
        """
        try:
            # Check terminal info - fast and reliable health check
            terminal_info = mt5.terminal_info()
            if terminal_info is None:
                logger.warning("MT5 health check failed: terminal_info returned None")
                return False
            
            # Check if connected
            if not terminal_info.connected:
                logger.warning("MT5 health check failed: not connected to trade server")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"MT5 health check exception: {e}")
            return False

    async def start(self):
        """
        Start the trading engine with MT5 connection.
        """
        logger.info(" Engine: Initializing Direct MT5 Connection (Monolith)...")
        
        if not self._init_mt5():
            logger.critical("Failed to initialize MT5. Engine not starting.")
            raise RuntimeError("MT5 initialization failed")
        
        logger.info(" MT5 Connected. Starting High-Speed Loop.")
        await self.run_tick_loop()

    async def run_tick_loop(self):
        """
        Main tick processing loop with health monitoring.
        """
        
        while self.running:
            try:
                # Periodic health check
                self.tick_count += 1
                if self.tick_count % self.HEALTH_CHECK_INTERVAL == 0:
                    if not self._check_mt5_health():
                        if not await self._reconnect_mt5():
                            # Failed to reconnect - exit to trigger watchdog restart
                            logger.critical("MT5 reconnection failed. Exiting for watchdog restart.")
                            raise RuntimeError("MT5 connection lost and could not reconnect")
                
                # 1. Collect all active symbols from all active users (orchestrators)
                all_orchestrators = list(self.bot_manager.bots.values())
                active_symbols = set()
                
                for orch in all_orchestrators:
                    active_symbols.update(orch.get_active_symbols())
                
                # 2. Iterate and Fetch
                if not active_symbols:
                    # Fallback to prevent tight loop if no bots
                    await asyncio.sleep(0.1)  # Small sleep when idle
                    continue

                for symbol in active_symbols:
                    # Ensure Symbol Selected (MT5 requirement)
                    if not mt5.symbol_select(symbol, True):
                        continue
                    
                    # Direct API Call - Zero Network Latency
                    tick = mt5.symbol_info_tick(symbol)
                    
                    if tick:
                        # Track stats
                        self.stats["ticks_processed"] += 1
                        self.stats["last_tick_time"] = datetime.now()
                        
                        # Get positions
                        positions = mt5.positions_get(symbol=symbol)
                        pos_count = len(positions) if positions else 0
                        
                        tick_data = {
                            'ask': tick.ask, 
                            'bid': tick.bid,
                            'positions_count': pos_count
                        }
                        
                        # Broadcast to all Orchestrators
                        tasks = [orch.on_external_tick(symbol, tick_data) for orch in all_orchestrators]
                        await asyncio.gather(*tasks)
                
                # Reset consecutive error counter on success
                self.consecutive_errors = 0
                        
            except Exception as e:
                self.consecutive_errors += 1
                self.stats["errors"] += 1
                logger.error(f"Engine tick error (#{self.consecutive_errors}): {e}")
                
                # If too many consecutive errors, try reconnecting
                if self.consecutive_errors >= 5:
                    logger.warning("Too many consecutive errors. Attempting MT5 reconnect...")
                    if not await self._reconnect_mt5():
                        raise RuntimeError("MT5 connection lost after consecutive errors")
                    self.consecutive_errors = 0
                
                await asyncio.sleep(1)  # Backoff on error
                
            # Minimal sleep for max performance but allow other async tasks
            await asyncio.sleep(0)

    async def stop(self):
        """
        Gracefully stop the engine.
        """
        logger.info("Stopping trading engine...")
        self.running = False
        mt5.shutdown()
        logger.info(" MT5 Disconnected. Engine stopped.")
        
    def get_stats(self) -> dict:
        """
        Get engine statistics for monitoring.
        """
        return {
            **self.stats,
            "tick_count": self.tick_count,
            "consecutive_errors": self.consecutive_errors,
            "running": self.running
        }