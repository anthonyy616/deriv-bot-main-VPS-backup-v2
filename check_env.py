import sys
import os

print(f"Python Executable: {sys.executable}")
print(f"CWD: {os.getcwd()}")
print(f"Path: {sys.path}")

try:
    import MetaTrader5 as mt5
    print(f"MetaTrader5 version: {mt5.__version__}")
except ImportError as e:
    print(f"Failed to import MetaTrader5: {e}")

try:
    import mt5_interface
    print("Successfully imported mt5_interface")
except ImportError as e:
    print(f"Failed to import mt5_interface: {e}")

try:
    from data.ingestion import DataIngestion
    print("Successfully imported DataIngestion")
except ImportError as e:
    print(f"Failed to import DataIngestion: {e}")
