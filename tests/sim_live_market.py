import sys
import os
# Ensure root directory is in python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import asyncio
import logging
from datetime import datetime
from collections import namedtuple
from dataclasses import dataclass

# 1. Import Real MT5 for Data Feed
try:
    import MetaTrader5 as real_mt5
except ImportError:
    print("MetaTrader5 module not found. Please install it.")
    sys.exit(1)

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("tests/simulation_results.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("LiveSim")

# 2. Define Mock MT5 Class
class MockEventBus:
    def publish(self, event_type, data):
        # logger.info(f"[EVENT BUS] {event_type} - {data}")
        pass

class MockSessionLogger:
    def __init__(self, logger):
        self.logger = logger
        
    def info(self, msg):
        self.logger.info(msg)
        
    def error(self, msg):
        self.logger.error(msg)
        
    def warning(self, msg):
        self.logger.warning(msg)
        
    def log_trade(self, symbol, pair_idx, direction, price, lot, trade_num, ticket):
        self.logger.info(f"[TRADE LOG] {symbol} | Pair {pair_idx} | {direction} @ {price} | Lot: {lot} | #{trade_num} | Ticket: {ticket}")

    def log_tp_sl(self, symbol, pair_idx, direction, result, profit):
        self.logger.info(f"[TP/SL LOG] {symbol} | Pair {pair_idx} | {direction} | Result: {result} | Profit: {profit}")

class MockMT5:
    def __init__(self):
        self.open_positions = []
        self.deals_history = []
        self._ticket_counter = 1000
        self.symbol_info_cache = {}
        
        # Copy constants from real MT5
        self._copy_constants()

    def _copy_constants(self):
        for attr in dir(real_mt5):
            if attr.isupper():
                setattr(self, attr, getattr(real_mt5, attr))

    def initialize(self):
        logger.info("[MOCK MT5] Initializing (connecting to Real MT5 for data)...")
        if not real_mt5.initialize():
            logger.error(f"Real MT5 Initialization failed: {real_mt5.last_error()}")
            return False
        return True

    def shutdown(self):
        real_mt5.shutdown()

    def symbol_select(self, symbol, enable):
        # [FIX] Added missing method
        # Proxy to Real MT5 to ensure we can get data for this symbol
        return real_mt5.symbol_select(symbol, enable)

    def symbol_info_tick(self, symbol):
        # Proxy to Real MT5 for LIVE DATA
        return real_mt5.symbol_info_tick(symbol)

    def symbol_info(self, symbol):
        # Proxy to Real MT5
        return real_mt5.symbol_info(symbol)
    
    def positions_total(self):
        return len(self.open_positions)

    def positions_get(self, symbol=None, ticket=None):
        # Return Fake Positions
        filtered = self.open_positions
        if symbol:
            filtered = [p for p in filtered if p.symbol == symbol]
        if ticket:
            filtered = [p for p in filtered if p.ticket == ticket]
            
        return tuple(filtered)

    def history_deals_get(self, date_from=None, date_to=None, position=None, ticket=None, symbol=None):
        # Return tuple of filtered deals
        filtered = self.deals_history
        if position:
            filtered = [d for d in filtered if d.position_id == position]
        
        if symbol:
            filtered = [d for d in filtered if d.symbol == symbol]
            
        # Return last 100 deals if no specific filter (simulates generic history fetch)
        if position is None and not filtered and not date_from:
             filtered = self.deals_history[-100:]
             
        return tuple(filtered)

    def order_send(self, request):
        # Simulate Order Execution
        action = request.get('action')
        symbol = request.get('symbol')
        volume = request.get('volume')
        price = request.get('price')
        sl = request.get('sl')
        tp = request.get('tp')
        type_ = request.get('type')
        magic = request.get('magic', 0)
        comment = request.get('comment', "")

        # Result Structure
        OrderSendResult = namedtuple('OrderSendResult', ['retcode', 'deal', 'order', 'volume', 'price', 'bid', 'ask', 'comment', 'request_id', 'retcode_external', 'request'])

        # Validate
        tick = self.symbol_info_tick(symbol)
        if not tick:
            logger.error("[MOCK_EXEC] Failed to get tick for execution")
            return OrderSendResult(10013, 0, 0, 0.0, 0.0, 0.0, 0.0, "No tick", 0, 0, request)

        if action == self.TRADE_ACTION_DEAL:
            # Market Order (Simulate Immediate Fill)
            ticket = self._ticket_counter
            self._ticket_counter += 1
            
            position_type = 0 if type_ == self.ORDER_TYPE_BUY else 1 # 0=BUY, 1=SELL
            
            # Create Mock Position Object
            @dataclass
            class MockPosition:
                ticket: int
                symbol: str
                type: int
                volume: float
                price_open: float
                sl: float
                tp: float
                price_current: float
                magic: int
                swap: float = 0.0
                profit: float = 0.0
                time: int = int(time.time())
                comment: str = ""
                identifier: int = 0
                
            new_pos = MockPosition(
                ticket=ticket,
                symbol=symbol,
                type=position_type,
                volume=volume,
                price_open=price,
                sl=sl,
                tp=tp,
                price_current=price, 
                magic=magic,
                comment=comment,
                identifier=ticket
            )
            
            self.open_positions.append(new_pos)
            logger.info(f"[SIM EXECUTION] OPEN {'BUY' if position_type==0 else 'SELL'} #{ticket} @ {price} vol={volume} SL={sl} TP={tp} Magic={magic}")
            
            return OrderSendResult(10009, ticket, ticket, volume, price, 0.0, 0.0, "Request executed", 0, 0, request)
            
        elif action == self.TRADE_ACTION_SLTP:
            # Modify SL/TP of existing position
            pos_ticket = request.get('position')
            for p in self.open_positions:
                if p.ticket == pos_ticket:
                    p.sl = sl
                    p.tp = tp
                    logger.info(f"[SIM EXECUTION] MODIFY #{pos_ticket} SL={sl} TP={tp}")
                    return OrderSendResult(10009, 0, 0, 0.0, 0.0, 0.0, 0.0, "Request executed", 0, 0, request)
            
            return OrderSendResult(10013, 0, 0, 0.0, 0.0, 0.0, 0.0, "Position not found", 0, 0, request)
        
        elif action == self.TRADE_ACTION_REMOVE:
            # Pending Order Remove (Simulate success)
            # Since this simple sim doesn't hold pending orders in a list, we just ack
            return OrderSendResult(10009, 0, 0, 0.0, 0.0, 0.0, 0.0, "Request executed", 0, 0, request)

        return OrderSendResult(10013, 0, 0, 0.0, 0.0, 0.0, 0.0, "Unsupported action", 0, 0, request)


# 3. MOCKS for Strategy Dependencies
class MockConfigManager:
    def __init__(self, config):
        self.config = config
        
    def get_symbol_config(self, symbol):
        # Return the grid settings for the symbol
        return self.config

    def get_config(self, key=None, default=None):
        if key == "global" or key is None:
            return {"global": {}}
        return self.config.get(key, default)
    
    def symbol_info(self, symbol):
        sinfo = real_mt5.symbol_info(symbol)
        if sinfo:
            return {
                "point": sinfo.point,
                "digits": sinfo.digits,
                "min_lot": sinfo.volume_min,
                "max_lot": sinfo.volume_max,
                "lot_step": sinfo.volume_step,
                "stops_level": sinfo.trade_stops_level,
                "contract_size": sinfo.trade_contract_size,
                "tick_size": sinfo.trade_tick_size,
                "tick_value": sinfo.trade_tick_value
            }
        return None

# 4. Global Patching
sim_mt5 = MockMT5()
sys.modules['MetaTrader5'] = sim_mt5

# 5. Import Strategy Engine (AFTER Patching)
# Try importing correctly based on your file structure
try:
    from core.engine.symbol_engine import SymbolEngine as GridStrategy
except ImportError:
    try:
        from core.strategy_engine import LadderGridStrategy as GridStrategy
    except ImportError:
        print("Could not import Strategy Engine. Check file paths.")
        sys.exit(1)

# 6. CONFIGURATION
TEST_CONFIG = {
    "SYMBOL": "FX Vol 20", # Change this to your target symbol
    "INITIAL_BALANCE": 10000.0,
    "GRID_SETTINGS": {
        "enabled": True,
        "spread": 20.0, # Adjust for Vol 10
        "max_pairs": 5,
        "max_positions": 5,
        "lot_sizes": [0.01, 0.02, 0.03, 0.04, 0.05],
        "buy_stop_tp": 24.0,
        "buy_stop_sl": 32.0,
        "sell_stop_tp": 24.0,
        "sell_stop_sl": 32.0,
        "hedge_enabled": True,
        "hedge_lot_size": 0.1
    }
}

# 7. Broker Simulation Logic (TP/SL Checker)
def check_broker_sim(tick):
    closed_indices = []
    bid = tick.bid
    ask = tick.ask
    
    for i, pos in enumerate(sim_mt5.open_positions):
        reason = None
        close_price = 0.0
        
        if pos.type == 0: # BUY
            if pos.tp > 0 and bid >= pos.tp:
                close_price = bid
                reason = real_mt5.DEAL_REASON_TP
            elif pos.sl > 0 and bid <= pos.sl:
                close_price = bid
                reason = real_mt5.DEAL_REASON_SL
                
        elif pos.type == 1: # SELL
            if pos.tp > 0 and ask <= pos.tp:
                close_price = ask
                reason = real_mt5.DEAL_REASON_TP
            elif pos.sl > 0 and ask >= pos.sl:
                close_price = ask
                reason = real_mt5.DEAL_REASON_SL

        if reason is not None:
            close_position(pos, close_price, reason)
            closed_indices.append(i)

    for i in sorted(closed_indices, reverse=True):
        sim_mt5.open_positions.pop(i)

def close_position(pos, close_price, reason):
    profit = 0.0
    if pos.type == 0: # BUY
        profit = (close_price - pos.price_open) * pos.volume
    else:
        profit = (pos.price_open - close_price) * pos.volume
        
    reason_str = "TP" if reason == real_mt5.DEAL_REASON_TP else "SL"
    logger.info(f"[SIM BROKER] {reason_str} #{pos.ticket} {pos.symbol} {'BUY' if pos.type==0 else 'SELL'} @ {pos.price_open} -> Closed @ {close_price} | PnL: {profit:.2f}")
    
    @dataclass
    class MockDeal:
        ticket: int; order: int; position_id: int; time: int; type: int; entry: int; symbol: str; volume: float; price: float; profit: float; reason: int; magic: int
        
    deal_exit = MockDeal(
        ticket=sim_mt5._ticket_counter,
        order=pos.ticket,
        position_id=pos.ticket,
        time=int(time.time()),
        type=1 if pos.type==0 else 0, # Deal type is opposite of position
        entry=1, # Entry Out
        symbol=pos.symbol,
        volume=pos.volume,
        price=close_price,
        profit=profit,
        reason=reason,
        magic=pos.magic
    )
    sim_mt5._ticket_counter += 1
    sim_mt5.deals_history.append(deal_exit)


# 8. Main Loop (Async)
async def async_main():
    logger.info("=== STARTING LIVE MARKET SIMULATION (Async) ===")
    
    if not sim_mt5.initialize():
        return

    # Init Config
    mock_config = MockConfigManager(TEST_CONFIG["GRID_SETTINGS"])
    symbol = TEST_CONFIG["SYMBOL"]
    
    # Init Strategy Engine
    engine = GridStrategy(
        config_manager=mock_config,
        symbol=symbol,
        session_logger=MockSessionLogger(logger)
    )
    
    engine.running = True 
    await engine.start() # Ensure DB inits

    logger.info("Engine Initialized. Starting Main Loop...")
    
    try:
        while True:
            # 1. Get Live Tick
            tick = sim_mt5.symbol_info_tick(symbol)
            if tick is None:
                logger.warning("No tick data received...")
                await asyncio.sleep(1)
                continue
            
            # 2. Broker Simulation
            check_broker_sim(tick)
            
            # 3. Feed Tick to Strategy (AS DICT)
            tick_dict = {
                'ask': tick.ask,
                'bid': tick.bid,
                'time': tick.time,
                'positions_count': sim_mt5.positions_total()
            }
            
            # Run Engine Tick Logic
            await engine.on_external_tick(tick_dict)
            
            await asyncio.sleep(0.1)
            
    except KeyboardInterrupt:
        logger.info("\nSimulation Stopped by User.")
    finally:
        sim_mt5.shutdown()

if __name__ == "__main__":
    try:
        if sys.platform == 'win32':
             asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass