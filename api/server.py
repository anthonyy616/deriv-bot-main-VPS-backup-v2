from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from core.bot_manager import BotManager
from core.trading_engine import TradingEngine
from core.mt5_credential_manager import MT5CredentialManager
from core.user_mt5_bridge import UserMT5Bridge
from supabase import create_client, Client
import asyncio
import os
import signal
import sys
from dotenv import load_dotenv
from cachetools import TTLCache 

load_dotenv()

# --- FRESH SESSION: Clean stale DB on boot ---
DB_PATH = "db/grid_v3.db"
if os.path.exists(DB_PATH):
    try:
        os.remove(DB_PATH)
        print(f"[STARTUP] Cleaned stale DB: {DB_PATH}")
    except Exception as e:
        print(f"[STARTUP] Could not clean DB (may be locked): {e}")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Auth Cache (30 seconds - shorter TTL for multi-user support)
auth_cache = TTLCache(maxsize=100, ttl=30)

# --- 1. Initialize Core Systems ---
bot_manager = BotManager()
trading_engine = TradingEngine(bot_manager)

@app.on_event("startup")
async def startup_event():
    print("[SERVER] Starting: Launching Monolith Engine...")
    asyncio.create_task(trading_engine.start())


# --- Pydantic Models for Config ---

class SymbolConfig(BaseModel):
    """Config for a single symbol"""
    enabled: Optional[bool] = None
    spread: Optional[float] = None
    max_pairs: Optional[int] = None      # Grid levels: 1, 3, 5, 7, 9
    max_positions: Optional[int] = None  # Trades per pair: 1-20
    lot_sizes: Optional[List[float]] = None
    buy_stop_tp: Optional[float] = None
    buy_stop_sl: Optional[float] = None
    sell_stop_tp: Optional[float] = None
    sell_stop_sl: Optional[float] = None
    hedge_enabled: Optional[bool] = None
    hedge_lot_size: Optional[float] = None

class GlobalConfig(BaseModel):
    """Global settings"""
    max_runtime_minutes: Optional[int] = None
    max_drawdown_usd: Optional[float] = None

class ConfigUpdate(BaseModel):
    """Multi-asset config update payload"""
    global_settings: Optional[GlobalConfig] = None
    symbols: Optional[Dict[str, SymbolConfig]] = None

class MT5LinkCredentials(BaseModel):
    """MT5 account linking payload"""
    login: str
    password: str
    server: str


# --- 2. Auth Helper ---
def verify_token_sync(token):
    """
    Verify Supabase token with short-term caching.
    Cache by token for 30 seconds to reduce API calls while allowing multiple users.
    """
    if token in auth_cache: 
        return auth_cache[token]
    
    try:
        user = supabase.auth.get_user(token)
        if user and user.user:
            auth_cache[token] = user
            return user
    except Exception as e:
        print(f"[AUTH] Token validation error: {e}")
        # Remove from cache if validation failed
        if token in auth_cache:
            del auth_cache[token]
    return None

async def get_current_user(request: Request):
    """
    Get the current authenticated user (just user info, not bot).
    Used for MT5 linking endpoints that don't need bot instance.
    """
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        raise HTTPException(401, "Missing token")
    
    try:
        token = auth_header.split(" ")[1]
        user = await asyncio.to_thread(verify_token_sync, token)
    except Exception as e:
        print(f"[AUTH] Check Failed: {e}")
        raise HTTPException(401, "Auth Validation Failed")
    
    if not user:
        raise HTTPException(401, "Invalid Token")
    
    return user.user

async def get_current_bot(request: Request):
    """
    Get or create bot instance for the authenticated user.
    Each user gets their own isolated bot instance.
    """
    auth_header = request.headers.get('Authorization')
    if not auth_header: 
        raise HTTPException(401, "Missing token")
    
    try:
        token = auth_header.split(" ")[1]
        user = await asyncio.to_thread(verify_token_sync, token)
    except Exception as e:
        print(f"[AUTH] Check Failed: {e}")
        raise HTTPException(401, "Auth Validation Failed")

    if not user: 
        raise HTTPException(401, "Invalid Token")
    
    # Each user gets their own bot instance (multi-tenant support)
    return await bot_manager.get_or_create_bot(user.user.id)

# --- 3. API Routes (Defined BEFORE Static Mount) ---

@app.get("/env")
async def get_env():
    return { "SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY }

@app.get("/config")
async def get_config(bot = Depends(get_current_bot)):
    """Get full multi-asset config"""
    return bot.config

@app.post("/config")
async def update_config(config: ConfigUpdate, bot = Depends(get_current_bot)):
    """Update multi-asset config"""
    update_data = {}
    
    # Handle global settings
    if config.global_settings:
        update_data["global"] = {
            k: v for k, v in config.global_settings.model_dump().items() 
            if v is not None
        }
    
    # Handle symbol-specific settings
    if config.symbols:
        update_data["symbols"] = {}
        for symbol, sym_cfg in config.symbols.items():
            sym_data = {k: v for k, v in sym_cfg.model_dump().items() if v is not None}
            if sym_data:
                update_data["symbols"][symbol] = sym_data
    
    updated = bot.config_manager.update_config(update_data)
    return updated


# --- Per-Symbol Control Endpoints ---

@app.post("/control/start")
async def start_all(bot = Depends(get_current_bot)):
    """Start all enabled symbols - always starts with fresh DB"""
    # Clean stale DB for fresh session
    if os.path.exists(DB_PATH):
        try:
            os.remove(DB_PATH)
            print(f"[START] Cleaned DB for fresh session: {DB_PATH}")
        except Exception as e:
            print(f"[START] Could not clean DB: {e}")
            return {
                "status": "blocked",
                "error": f"DB file locked ({e}). Please click on terminate all to start bot."
            }
    
    await bot.start()
    return {"status": "started", "symbols": bot.config_manager.get_enabled_symbols()}

@app.post("/control/stop")
async def stop_all(bot = Depends(get_current_bot)):
    """Stop all symbols"""
    await bot.stop()
    return {"status": "stopped"}

@app.post("/control/start/{symbol}")
async def start_symbol(symbol: str, bot = Depends(get_current_bot)):
    """Start a specific symbol"""
    # Clean stale DB for fresh session
    if os.path.exists(DB_PATH):
        try:
            os.remove(DB_PATH)
            print(f"[START] Cleaned DB for fresh session: {DB_PATH}")
        except Exception as e:
            print(f"[START] Could not clean DB: {e}")
            return {
                "status": "blocked",
                "error": f"DB file locked ({e}). Please terminate all or restart bot."
            }
    
    # Enable the symbol first
    bot.config_manager.enable_symbol(symbol, True)
    await bot.start_symbol(symbol)
    return {"status": "started", "symbol": symbol}

@app.post("/control/stop/{symbol}")
async def stop_symbol(symbol: str, bot = Depends(get_current_bot)):
    """Stop a specific symbol"""
    await bot.stop_symbol(symbol)
    return {"status": "stopped", "symbol": symbol}

@app.post("/control/terminate/{symbol}")
async def terminate_symbol(symbol: str, bot = Depends(get_current_bot)):
    """Nuclear reset - close all positions for a symbol immediately"""
    await bot.terminate_symbol(symbol)
    return {"status": "terminated", "symbol": symbol}

@app.post("/control/terminate-all")
async def terminate_all(bot = Depends(get_current_bot)):
    """Nuclear reset - close all positions for all symbols and clean DB"""
    await bot.terminate_all()
    
    # Clean DB after termination for complete reset
    db_cleaned = True
    db_warning = None
    
    if os.path.exists(DB_PATH):
        try:
            os.remove(DB_PATH)
            print(f"[TERMINATE] Cleaned DB after nuclear reset: {DB_PATH}")
        except Exception as e:
            print(f"[TERMINATE] Could not clean DB: {e}")
            db_cleaned = False
            db_warning = f"Could not delete DB file ({e}). Please click on terminate all again."
    
    return {
        "status": "terminated_all",
        "db_cleaned": db_cleaned,
        "warning": db_warning
    }

@app.get("/status")
async def get_status(bot = Depends(get_current_bot)):
    """Get status for all active strategies"""
    return bot.get_status()

# --- History Endpoints ---

@app.get("/history")
async def get_history(bot = Depends(get_current_bot)):
    """Get list of session history files for this user"""
    sessions = bot.session_logger.get_sessions()
    return sessions

@app.get("/history/{session_id}")
async def get_session_log(session_id: str, bot = Depends(get_current_bot)):
    """Get contents of a specific session log"""
    content = bot.session_logger.get_session_content(session_id)
    if content:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(content)
    raise HTTPException(404, "Session not found")

# --- MT5 Account Linking Endpoints ---

# Initialize credential manager (lazy - will be created when first used)
_credential_manager = None

def get_credential_manager():
    global _credential_manager
    if _credential_manager is None:
        _credential_manager = MT5CredentialManager(supabase)
    return _credential_manager

@app.get("/mt5/status")
async def get_mt5_status(user = Depends(get_current_user)):
    """Check if user has linked MT5 account"""
    cm = get_credential_manager()
    creds = await cm.get_credentials(user.id)
    
    if creds:
        return {
            "linked": True,
            "login": creds.login,
            "server": creds.server,
            "is_connected": creds.is_connected
        }
    return {"linked": False}

@app.post("/mt5/link")
async def link_mt5_account(credentials: MT5LinkCredentials, user = Depends(get_current_user)):
    """
    Link MT5 account to user. Validates credentials before saving.
    Each MT5 account can only be linked to ONE user.
    """
    cm = get_credential_manager()
    
    # Check if MT5 is already linked to another user
    is_available = await cm.is_mt5_available(credentials.login, credentials.server, user.id)
    if not is_available:
        raise HTTPException(400, f"MT5 account {credentials.login}@{credentials.server} is already linked to another user")
    
    # Validate credentials by attempting connection
    mt5_path = os.getenv("MT5_PATH", "")
    success, message = UserMT5Bridge.validate_credentials(
        int(credentials.login),
        credentials.password,
        credentials.server,
        mt5_path
    )
    
    if not success:
        raise HTTPException(400, f"MT5 validation failed: {message}")
    
    # Save encrypted credentials
    try:
        saved = await cm.save_credentials(
            user.id,
            credentials.login,
            credentials.password,
            credentials.server
        )
        return {
            "status": "linked",
            "login": credentials.login,
            "server": credentials.server,
            "message": message
        }
    except Exception as e:
        raise HTTPException(400, str(e))

@app.delete("/mt5/unlink")
async def unlink_mt5_account(user = Depends(get_current_user)):
    """Unlink MT5 account from user"""
    cm = get_credential_manager()
    
    # Stop any running bots first
    bot = bot_manager.get_bot(user.id)
    if bot:
        await bot.terminate_all()
        await bot_manager.stop_bot(user.id)
    
    # Delete credentials
    deleted = await cm.delete_credentials(user.id)
    
    if deleted:
        return {"status": "unlinked", "message": "MT5 account unlinked successfully"}
    return {"status": "not_linked", "message": "No MT5 account was linked"}

# Mount static folder for assets (css/js images)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Serve UI at Root
@app.get("/")
async def read_index():
    return FileResponse('static/index.html')


# --- 4. Simplified Signal Handling ---
def cleanup_handler(signum, frame):
    """Handle SIGINT (Ctrl+C) - exit cleanly. DB cleanup handled on next startup."""
    print("\n[SERVER] Caught Signal. Exiting...")
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup_handler)
print("[SERVER] Signal Handler Registered")