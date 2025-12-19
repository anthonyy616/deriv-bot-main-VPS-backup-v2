import logging
from collections import deque
import numpy as np
from core.event_bus import EventBus, EventType

class FeatureStore:
    def __init__(self, buffer_size=1000):
        self.buffer_size = buffer_size
        self.ticks = deque(maxlen=buffer_size)
        self.event_bus = None
        
        # Pre-calculated features
        self.inter_tick_durations = deque(maxlen=buffer_size)

    def set_event_bus(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.event_bus.subscribe(EventType.TICK, self.on_tick)

    def on_tick(self, event):
        """Updates the store with a new tick."""
        tick = event.payload
        self.ticks.append(tick)
        
        # Calculate basic features immediately
        self._calculate_features(tick)

    def _calculate_features(self, tick):
        """Calculates microstructural features."""
        if len(self.ticks) > 1:
            prev_tick = self.ticks[-2]
            duration = tick['time_msc'] - prev_tick['time_msc']
            self.inter_tick_durations.append(duration)
        else:
            self.inter_tick_durations.append(0)

    def get_latest_features(self):
        """Returns a dictionary of the latest features."""
        if not self.ticks:
            return None
            
        return {
            'last_price': self.ticks[-1]['ask'], # Using ask as reference, or mid
            'spread': self.ticks[-1]['ask'] - self.ticks[-1]['bid'],
            'inter_tick_duration': self.inter_tick_durations[-1] if self.inter_tick_durations else 0
        }
