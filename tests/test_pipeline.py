import asyncio
import logging
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.trading_engine import TradingEngine as Engine
from data.ingestion import DataIngestion
from data.feature_store import FeatureStore
from mt5_interface import MT5Interface
import config

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

async def main():
    # Initialize MT5
    mt5_client = MT5Interface(
        login=config.MT5_LOGIN,
        password=config.MT5_PASSWORD,
        server=config.MT5_SERVER
    )
    
    if not mt5_client.start():
        logging.error("Failed to start MT5")
        return

    # Initialize Components
    engine = Engine()
    
    ingestion = DataIngestion(mt5_client, config.SYMBOL)
    feature_store = FeatureStore()
    
    # Register Components
    engine.register_component(ingestion)
    engine.register_component(feature_store)
    
    # Start Engine
    # Run for 10 seconds then stop
    try:
        task = asyncio.create_task(engine.start())
        
        logging.info("Running for 10 seconds...")
        await asyncio.sleep(10)
        
        # Check if we got features
        features = feature_store.get_latest_features()
        if features:
            logging.info(f"Latest Features: {features}")
        else:
            logging.warning("No features captured!")
            
        await engine.shutdown(None, []) # Simple shutdown for test
        task.cancel()
        
    except Exception as e:
        logging.error(f"Test failed: {e}")
    finally:
        mt5_client.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
