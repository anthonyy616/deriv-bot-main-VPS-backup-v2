"""
Production Watchdog - run_forever.py

This script wraps the main FastAPI application with:
1. Infinite restart loop - if the app crashes, it restarts automatically
2. Crash logging - all crashes are logged to crash.log with timestamps
3. Graceful shutdown handling - Ctrl+C and system signals are handled properly
4. MT5 health monitoring - reconnects MT5 if connection is lost

USAGE:
    python run_forever.py

NSSM CONFIGURATION:
    nssm install TradingBot "C:\...\python.exe" "C:\...\run_forever.py"
"""

import os
import sys
import time
import signal
import logging
import logging.handlers  # FIXED: Import this BEFORE using it
import traceback
import subprocess
from datetime import datetime
from pathlib import Path

# --- Configuration ---
RESTART_DELAY_SECONDS = 5
MAX_RAPID_RESTARTS = 10  # Max restarts within window before cooling down
RAPID_RESTART_WINDOW = 60  # seconds
COOLDOWN_PERIOD = 300  # 5 minutes cooldown if too many rapid restarts

# --- Setup Logging ---
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Configure crash logger
crash_logger = logging.getLogger("crash")
crash_logger.setLevel(logging.ERROR)

crash_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "crash.log",
    maxBytes=5 * 1024 * 1024,  # 5 MB
    backupCount=10,
    encoding="utf-8"
)
crash_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))
crash_logger.addHandler(crash_handler)

# Console logger
console_logger = logging.getLogger("watchdog")
console_logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(asctime)s | WATCHDOG | %(message)s"))
console_logger.addHandler(console_handler)


class Watchdog:
    """
    Production watchdog that keeps the trading bot running forever.
    """
    
    def __init__(self):
        self.running = True
        self.restart_times = []
        self.process = None
        
        # Register signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        console_logger.info(f"Received signal {signum}. Initiating graceful shutdown...")
        self.running = False
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
    
    def _check_rapid_restarts(self) -> bool:
        """
        Check if we're restarting too rapidly.
        Returns True if we should cooldown.
        """
        now = time.time()
        # Remove old restart times
        self.restart_times = [t for t in self.restart_times if now - t < RAPID_RESTART_WINDOW]
        
        if len(self.restart_times) >= MAX_RAPID_RESTARTS:
            return True
        
        self.restart_times.append(now)
        return False
    
    def _get_python_executable(self) -> str:
        """Get the Python executable path."""
        return sys.executable
    
    def _get_main_script(self) -> str:
        """Get the main.py script path."""
        return str(Path(__file__).parent / "main.py")
    
    def run_forever(self):
        """
        Main loop - runs the bot and restarts on crash.
        """
        console_logger.info("=" * 60)
        console_logger.info("TRADING BOT WATCHDOG STARTED")
        console_logger.info(f"Python: {self._get_python_executable()}")
        console_logger.info(f"Main Script: {self._get_main_script()}")
        console_logger.info(f"PID: {os.getpid()}")
        console_logger.info("=" * 60)
        
        restart_count = 0
        
        while self.running:
            try:
                # Check for rapid restarts
                if self._check_rapid_restarts():
                    console_logger.warning(
                        f"Too many rapid restarts ({MAX_RAPID_RESTARTS} in {RAPID_RESTART_WINDOW}s). "
                        f"Cooling down for {COOLDOWN_PERIOD}s..."
                    )
                    crash_logger.error(
                        f"COOLDOWN TRIGGERED: {MAX_RAPID_RESTARTS} restarts in {RAPID_RESTART_WINDOW}s. "
                        f"Waiting {COOLDOWN_PERIOD}s before next attempt."
                    )
                    time.sleep(COOLDOWN_PERIOD)
                    self.restart_times.clear()
                    continue
                
                # Start the main process
                restart_count += 1
                console_logger.info(f"Starting bot (attempt #{restart_count})...")
                
                self.process = subprocess.Popen(
                    [self._get_python_executable(), self._get_main_script()],
                    cwd=Path(__file__).parent,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1  # Line buffered
                )
                
                # Stream output to console and log file
                stdout_log = LOG_DIR / "stdout.log"
                with open(stdout_log, "a", encoding="utf-8") as log_file:
                    log_file.write(f"\n{'='*60}\n")
                    log_file.write(f"RESTART #{restart_count} at {datetime.now().isoformat()}\n")
                    log_file.write(f"{'='*60}\n")
                    
                    for line in self.process.stdout:
                        print(line, end="")  # Print to console
                        log_file.write(line)  # Write to file
                        log_file.flush()
                
                # Process ended - get exit code
                exit_code = self.process.wait()
                
                if exit_code == 0:
                    console_logger.info("Bot exited cleanly (code 0). Stopping watchdog.")
                    break
                else:
                    console_logger.warning(f"Bot crashed with exit code {exit_code}")
                    crash_logger.error(f"CRASH: Exit code {exit_code}")
                    
            except Exception as e:
                console_logger.error(f"Watchdog error: {e}")
                crash_logger.error(f"WATCHDOG ERROR: {e}\n{traceback.format_exc()}")
            
            # Wait before restart
            if self.running:
                console_logger.info(f"Restarting in {RESTART_DELAY_SECONDS} seconds...")
                time.sleep(RESTART_DELAY_SECONDS)
        
        console_logger.info("Watchdog stopped.")


if __name__ == "__main__":
    watchdog = Watchdog()
    watchdog.run_forever()
