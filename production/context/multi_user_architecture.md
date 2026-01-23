# Multi-User MT5 Architecture - Status & Suggestions

## Current Implementation Summary

We have adapted the codebase to support multiple users by allowing each user to link their own MT5 account.

### Core Components Implemented

1. **Database Layer**: `supabase_multi_user_schema.sql` creates a `user_mt5_credentials` table with Row Level Security (RLS) so users only access their own data.
2. **Encryption**: `MT5CredentialManager` handles AES-128 encryption of MT5 passwords using a server-side `MT5_ENCRYPTION_KEY`.
3. **Connection Management**: `UserMT5Bridge` manages connections. It currently handles sequential switching (shutting down one connection to start another).
4. **API Endpoints**:
    - `/mt5/status`: Check if linked.
    - `/mt5/link`: Validates credentials and stores them.
    - `/mt5/unlink`: Removes account from database.
5. **Frontend**: Added an MT5 linking modal and an "Unlink" button in the dashboard.

---

## The "MT5 Singleton" Limitation

**Critical Issue**: The MT5 Python library (`MetaTrader5`) is a singleton that controls the local MT5 desktop app. The desktop app can only be logged into **one** account at a time.

### Current Implementation (Sequential)

- If User A starts trading, the bot logs MT5 into Account A.
- If User B starts trading, the bot logs MT5 into Account B, **disconnecting User A**.
- **Suitable for**: Applications where users don't trade concurrently, or for a single owner managing different accounts sequentially.

---

## Future Architecture Recommendations (Concurrent Trading)

To achieve true concurrent trading (multiple users trading at the exact same time), we need one of the following:

### 1. Process-Per-User (Standard Production Path)

Run each user's trading bot in a separate OS process. Each process points to a separate "Portable" MT5 installation on the VPS.

- **How**: The main API server spawns `subprocess.Popen(["python", "worker.py", "--user-id", "..."])`.
- **Infrastructure**: You would need several MT5 folders (Terminal1, Terminal2, etc.) on the disk.

### 2. Multi-Terminal Bridge

Use a 3rd party bridge (like a ZeroMQ or Socket-based EA) that runs inside MT5 and communicates with Python. Each MT5 terminal would run the EA.

### 3. VPS-Per-User (Simplest Scaling)

If you are charging users for this service, the most robust way is to give each user their own tiny VPS instance. This provides 100% isolation and security.

---

## Deployment Checklist

1. Run the SQL schema in Supabase.
2. Generate `MT5_ENCRYPTION_KEY` via `cryptography` and add to `.env`.
3. Update `requirements.txt` on the VPS.
4. Copy the new `multi user` folder and updated `api/server.py` and `static/index.html`.
