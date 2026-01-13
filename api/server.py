from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from core.bot_manager import BotManager
from core.engine import TradingEngine 
from supabase import create_client, Client
import asyncio
import os
from dotenv import load_dotenv
from cachetools import TTLCache 

load_dotenv()

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
    """Start all enabled symbols"""
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
    """Nuclear reset - close all positions for all symbols"""
    await bot.terminate_all()
    return {"status": "terminated_all"}

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

# Mount static folder for assets (css/js images)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Serve UI at Root
@app.get("/")
async def read_index():
    return FileResponse('static/index.html')