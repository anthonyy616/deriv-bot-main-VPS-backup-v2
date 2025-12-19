import uvicorn
import os
import sys
import signal
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# --- Production Logging Setup ---
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        RotatingFileHandler(
            LOG_DIR / "bot.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8"
        ),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("main")

# --- Import App ---
from api.server import app

# --- Configuration ---
# Default to production settings, override with environment variables
HOST = os.getenv("BOT_HOST", "0.0.0.0")
PORT = int(os.getenv("BOT_PORT", "800"))

# --- Graceful Shutdown ---
def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    logger.info(f"Received signal {signum}. Initiating graceful shutdown...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


if __name__ == "__main__":
    # Ensure the root directory is in the python path
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    
    logger.info("=" * 60)
    logger.info(" TRADING BOT STARTING")
    logger.info("=" * 60)
    logger.info(f"ENTRY POINT: api/server.py")
    logger.info(f"ENGINE:      Polling Mode (Active)")
    logger.info(f"HOST:        {HOST}")
    logger.info(f"PORT:        {PORT}")
    logger.info(f"PID:         {os.getpid()}")
    logger.info(f"PYTHON:      {sys.executable}")
    logger.info(f"CWD:         {os.getcwd()}")
    logger.info("=" * 60)

    try:
        uvicorn.run(
            app, 
            host=HOST, 
            port=PORT,
            log_level="info",
            access_log=True
        )
    except KeyboardInterrupt:
        logger.info(" Shutdown requested by user (Ctrl+C)")
    except Exception as e:
        logger.critical(f" FATAL ERROR: {e}", exc_info=True)
        raise  # Re-raise to trigger watchdog restart
    finally:
        logger.info(" Bot stopped.")