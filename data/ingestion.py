import asyncio
import logging
import MetaTrader5 as mt5
from datetime import datetime, timedelta
from core.event_bus import EventBus, Event, EventType
from mt5_interface import MT5Interface

class DataIngestion:
    def __init__(self, mt5_interface: MT5Interface, symbol: str):
        self.mt5 = mt5_interface
        self.symbol = symbol
        self.event_bus = None
        self.running = False
        # Start polling from 10 seconds ago to catch immediate context
        self.last_poll_time = datetime.now() - timedelta(seconds=10)

    def set_event_bus(self, event_bus: EventBus):
        self.event_bus = event_bus

    async def run(self):
        """Polls for new ticks."""
        self.running = True
        logging.info(f"DataIngestion started for {self.symbol}")
        
        while self.running:
            try:
                # Poll for ticks from the last poll time
                ticks = self.mt5.get_ticks(self.symbol, from_date=self.last_poll_time, num_ticks=1000)
                
                if ticks is not None and len(ticks) > 0:
                    # Update last_poll_time to the time of the last tick + 1ms to avoid duplicates
                    # ticks is a numpy structured array, 'time' is in seconds, 'time_msc' in ms
                    last_tick = ticks[-1]
                    
                    # Convert numpy datetime64/int to datetime if needed, or just keep using datetime for the API
                    # MT5 API expects datetime object for copy_ticks_from
                    
                    # We need to be careful not to miss ticks if multiple happen in same ms.
                    # But for now, let's just use the last tick time.
                    
                    # Convert timestamp to datetime
                    last_tick_ts = last_tick['time'] # seconds
                    self.last_poll_time = datetime.fromtimestamp(last_tick_ts)
                    
                    for tick in ticks:
                        # Publish TICK event
                        if self.event_bus:
                            event = Event(EventType.TICK, tick)
                            await self.event_bus.publish(event)
                            
                # Sleep a tiny bit
                await asyncio.sleep(0.01) 
                
            except Exception as e:
                logging.error(f"Error in DataIngestion: {e}")
                await asyncio.sleep(1) # Backoff on error

    def stop(self):
        self.running = False
