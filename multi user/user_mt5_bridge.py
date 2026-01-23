"""
Multi-User MT5 Bridge
Individual MT5 connection per user for isolated trading.
"""

import MetaTrader5 as mt5
import logging
import threading
from typing import Optional, Dict, Any
from dataclasses import dataclass

logger = logging.getLogger("mt5_bridge")


@dataclass
class MT5ConnectionInfo:
    """Stores MT5 connection state for a user"""
    login: int
    server: str
    connected: bool = False
    last_error: str = ""


class UserMT5Bridge:
    """
    Per-user MT5 connection manager.
    
    IMPORTANT: MT5's Python API is a global singleton - only ONE connection
    can be active at a time. For true multi-user support, you need:
    
    Option A: Process-per-user (recommended for production)
        - Each user runs in a separate Python process
        - Each process has its own MT5 connection
        
    Option B: MT5 Manager API (enterprise)
        - Use MT5's Manager API for multi-account support
        - Requires broker partnership
        
    Option C: Connection switching (current implementation)
        - Switch MT5 connections between users
        - Not suitable for concurrent trading
        
    This implementation uses Option C for development, but logs warnings
    to upgrade to Option A for production.
    """
    
    # Class-level lock for MT5 connection (it's a global singleton)
    _connection_lock = threading.Lock()
    _current_user: Optional[str] = None
    
    def __init__(self, user_id: str, login: int, password: str, server: str, mt5_path: str = ""):
        self.user_id = user_id
        self.login = login
        self.password = password
        self.server = server
        self.mt5_path = mt5_path
        self.connected = False
        self.last_error = ""
    
    def connect(self) -> bool:
        """
        Connect to MT5 with this user's credentials.
        
        WARNING: This disconnects any existing MT5 connection!
        MT5's Python API only supports one connection at a time.
        """
        with self._connection_lock:
            # Check if already connected as this user
            if UserMT5Bridge._current_user == self.user_id and self._is_connected():
                return True
            
            # Log warning about connection switching
            if UserMT5Bridge._current_user and UserMT5Bridge._current_user != self.user_id:
                logger.warning(
                    f"Switching MT5 from user {UserMT5Bridge._current_user} to {self.user_id}. "
                    "For production, use process-per-user architecture."
                )
            
            try:
                # Shutdown any existing connection
                mt5.shutdown()
                
                # Initialize with path if provided
                init_kwargs = {}
                if self.mt5_path:
                    init_kwargs["path"] = self.mt5_path
                
                if not mt5.initialize(**init_kwargs):
                    error = mt5.last_error()
                    self.last_error = f"MT5 init failed: {error}"
                    logger.error(self.last_error)
                    return False
                
                # Login with user's credentials
                if not mt5.login(self.login, password=self.password, server=self.server):
                    error = mt5.last_error()
                    self.last_error = f"MT5 login failed: {error}"
                    logger.error(self.last_error)
                    mt5.shutdown()
                    return False
                
                self.connected = True
                self.last_error = ""
                UserMT5Bridge._current_user = self.user_id
                
                logger.info(f"[MT5] User {self.user_id} connected as {self.login}@{self.server}")
                return True
                
            except Exception as e:
                self.last_error = f"MT5 connection exception: {e}"
                logger.error(self.last_error)
                return False
    
    def disconnect(self):
        """Disconnect from MT5"""
        with self._connection_lock:
            if UserMT5Bridge._current_user == self.user_id:
                mt5.shutdown()
                UserMT5Bridge._current_user = None
                self.connected = False
                logger.info(f"[MT5] User {self.user_id} disconnected")
    
    def _is_connected(self) -> bool:
        """Check if MT5 is currently connected"""
        try:
            terminal = mt5.terminal_info()
            return terminal is not None and terminal.connected
        except:
            return False
    
    def ensure_connected(self) -> bool:
        """Ensure connection is active, reconnect if needed"""
        if UserMT5Bridge._current_user == self.user_id and self._is_connected():
            return True
        return self.connect()
    
    def get_account_info(self) -> Optional[Dict[str, Any]]:
        """Get MT5 account information"""
        if not self.ensure_connected():
            return None
        
        account = mt5.account_info()
        if account:
            return {
                "login": account.login,
                "balance": account.balance,
                "equity": account.equity,
                "profit": account.profit,
                "server": account.server,
                "currency": account.currency
            }
        return None
    
    @classmethod
    def validate_credentials(cls, login: int, password: str, server: str, mt5_path: str = "") -> tuple[bool, str]:
        """
        Validate MT5 credentials by attempting a connection.
        
        Returns:
            (success: bool, message: str)
        """
        with cls._connection_lock:
            try:
                # Store current user to restore later
                previous_user = cls._current_user
                
                # Shutdown existing connection
                mt5.shutdown()
                
                # Initialize
                init_kwargs = {}
                if mt5_path:
                    init_kwargs["path"] = mt5_path
                
                if not mt5.initialize(**init_kwargs):
                    error = mt5.last_error()
                    return False, f"MT5 initialization failed: {error}"
                
                # Try login
                if not mt5.login(login, password=password, server=server):
                    error = mt5.last_error()
                    mt5.shutdown()
                    return False, f"MT5 login failed: {error}"
                
                # Get account info for confirmation
                account = mt5.account_info()
                if not account:
                    mt5.shutdown()
                    return False, "Could not retrieve account info"
                
                # Disconnect test connection
                mt5.shutdown()
                cls._current_user = None
                
                return True, f"Successfully connected to {account.login}@{account.server}"
                
            except Exception as e:
                mt5.shutdown()
                return False, f"Validation error: {e}"


class MultiUserMT5Manager:
    """
    Manages MT5 connections for multiple users.
    
    For production deployment with concurrent users, this should be
    replaced with a process-per-user architecture where each user's
    bot runs in an isolated Python process with its own MT5 connection.
    """
    
    def __init__(self):
        self.bridges: Dict[str, UserMT5Bridge] = {}
    
    def create_bridge(
        self, 
        user_id: str, 
        login: int, 
        password: str, 
        server: str,
        mt5_path: str = ""
    ) -> UserMT5Bridge:
        """Create or get an MT5 bridge for a user"""
        if user_id in self.bridges:
            # Update credentials if they changed
            bridge = self.bridges[user_id]
            if bridge.login != login or bridge.server != server:
                bridge.login = login
                bridge.password = password
                bridge.server = server
                bridge.connected = False  # Force reconnect
        else:
            bridge = UserMT5Bridge(user_id, login, password, server, mt5_path)
            self.bridges[user_id] = bridge
        
        return bridge
    
    def get_bridge(self, user_id: str) -> Optional[UserMT5Bridge]:
        """Get existing bridge for a user"""
        return self.bridges.get(user_id)
    
    def remove_bridge(self, user_id: str):
        """Remove and disconnect a user's bridge"""
        if user_id in self.bridges:
            self.bridges[user_id].disconnect()
            del self.bridges[user_id]
    
    def get_active_user(self) -> Optional[str]:
        """Get the currently active MT5 user"""
        return UserMT5Bridge._current_user


# Global manager instance
mt5_manager = MultiUserMT5Manager()
