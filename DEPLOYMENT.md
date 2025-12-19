# Production Deployment Guide

## Quick Start

### 1. Copy Files to VPS
Copy the entire `trade-bot-deriv` folder to:
```
C:\Users\Administrator\Downloads\trade-bot-deriv
```

### 2. Configure Environment
1. Copy `.env.production` to `.env`:
   ```
   copy .env.production .env
   ```

2. Edit `.env` with your credentials:
   ```
   SUPABASE_URL=https://your-project.supabase.co
   SUPABASE_KEY=your-anon-key
   MT5_LOGIN=your-mt5-login
   MT5_PASSWORD=your-mt5-password
   MT5_SERVER=Weltrade-Demo  (or your server name)
   MT5_PATH=C:\Program Files\Weltrade MT5\terminal64.exe
   BOT_HOST=0.0.0.0
   BOT_PORT=800
   ```

### 3. Install Dependencies
```cmd
cd C:\Users\Administrator\Downloads\trade-bot-deriv-v2
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe -m pip install -r requirements.txt
```

### 4. Create Logs Directory
```cmd
mkdir logs
```

### 5. Install Windows Service
Run as Administrator:
```cmd
install_service.bat
```

### 6. Verify Service
```cmd
sc query TradingBotService
```

Open browser: http://45.144.242.97:800/

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                   NSSM Service                          │
│  - Starts on Windows boot                               │
│  - Restarts on crash (5 second delay)                   │
│  - Logs to logs/service_stdout.log                      │
│                                                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │           run_forever.py (Watchdog)               │  │
│  │  - Catches Python exceptions                      │  │
│  │  - Restarts app on crash                          │  │
│  │  - Cooldown if too many rapid restarts            │  │
│  │  - Logs to logs/crash.log                         │  │
│  │                                                   │  │
│  │  ┌─────────────────────────────────────────────┐  │  │
│  │  │            main.py (FastAPI)                │  │  │
│  │  │  - Web server on port 800                   │  │  │
│  │  │  - Logs to logs/bot.log                     │  │  │
│  │  │                                             │  │  │
│  │  │  ┌───────────────────────────────────────┐  │  │  │
│  │  │  │    core/engine.py (MT5 Engine)        │  │  │  │
│  │  │  │  - Health check every 100 ticks       │  │  │  │
│  │  │  │  - Auto-reconnect (10 attempts)       │  │  │  │
│  │  │  │  - 5 second retry delay               │  │  │  │
│  │  │  └───────────────────────────────────────┘  │  │  │
│  │  └─────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

---

## Logs

| File | Purpose |
|------|---------|
| `logs/bot.log` | Main application logs (rotating, 10MB x 5) |
| `logs/crash.log` | Crash reports from watchdog |
| `logs/stdout.log` | Real-time console output |
| `logs/service_stdout.log` | NSSM service stdout |
| `logs/service_stderr.log` | NSSM service stderr |

---

## Commands

### Start Service
```cmd
net start TradingBotService
```

### Stop Service
```cmd
net stop TradingBotService
```

### Check Status
```cmd
sc query TradingBotService
```

### View Logs (PowerShell)
```powershell
Get-Content logs\bot.log -Tail 50 -Wait
```

### Reinstall Service
```cmd
uninstall_service.bat
install_service.bat
```

---

## Troubleshooting

### Bot not taking trades
1. Check `logs/bot.log` for errors
2. Verify MT5 is running and logged in
3. Check `logs/crash.log` for crash reports

### Website not responding
1. Check if service is running: `sc query TradingBotService`
2. Check firewall: Port 800 must be open for TCP
3. Check `logs/service_stderr.log` for errors

### MT5 disconnection
The engine auto-reconnects. Check `logs/bot.log` for:
```
⚠️ MT5 connection lost. Attempting reconnection...
✅ MT5 reconnected on attempt X
```

---

## Updating Code

1. Stop service: `net stop TradingBotService`
2. Copy new files to VPS
3. Start service: `net start TradingBotService`
