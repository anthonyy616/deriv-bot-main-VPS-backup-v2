-- ============================================
-- MULTI-USER MT5 SCHEMA
-- ============================================
-- Run this in Supabase SQL Editor to set up multi-user support
-- Each user can link ONE MT5 account, and each MT5 account can only be linked to ONE user
-- ============================================
-- 1. USER MT5 CREDENTIALS TABLE
-- ============================================
-- Stores encrypted MT5 credentials per user
-- Password is encrypted server-side before storage
CREATE TABLE IF NOT EXISTS user_mt5_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    mt5_login VARCHAR(50) NOT NULL,
    mt5_password_encrypted TEXT NOT NULL,
    -- Encrypted with Fernet (AES-128-CBC)
    mt5_server VARCHAR(100) NOT NULL,
    is_connected BOOLEAN DEFAULT FALSE,
    -- True when actively trading
    last_connected_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    -- CONSTRAINTS: 1 user = 1 MT5, 1 MT5 = 1 user
    CONSTRAINT unique_user_mt5 UNIQUE(user_id),
    CONSTRAINT unique_mt5_account UNIQUE(mt5_login, mt5_server)
);
-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_user_mt5_user_id ON user_mt5_credentials(user_id);
CREATE INDEX IF NOT EXISTS idx_user_mt5_login ON user_mt5_credentials(mt5_login, mt5_server);
-- ============================================
-- 2. ROW LEVEL SECURITY (RLS)
-- ============================================
-- Users can only access their own credentials
ALTER TABLE user_mt5_credentials ENABLE ROW LEVEL SECURITY;
-- Policy: Users can SELECT their own credentials
CREATE POLICY "Users can view own credentials" ON user_mt5_credentials FOR
SELECT USING (auth.uid() = user_id);
-- Policy: Users can INSERT their own credentials
CREATE POLICY "Users can insert own credentials" ON user_mt5_credentials FOR
INSERT WITH CHECK (auth.uid() = user_id);
-- Policy: Users can UPDATE their own credentials
CREATE POLICY "Users can update own credentials" ON user_mt5_credentials FOR
UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
-- Policy: Users can DELETE their own credentials
CREATE POLICY "Users can delete own credentials" ON user_mt5_credentials FOR DELETE USING (auth.uid() = user_id);
-- ============================================
-- 3. USER TRADING CONFIGS TABLE
-- ============================================
-- Per-user trading configuration (symbols, lot sizes, etc.)
-- This replaces file-based config storage
CREATE TABLE IF NOT EXISTS user_trading_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    config_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Full config as JSON
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT unique_user_config UNIQUE(user_id)
);
-- RLS for trading configs
ALTER TABLE user_trading_configs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can manage own config" ON user_trading_configs FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
-- ============================================
-- 4. TRIGGER: Auto-update updated_at
-- ============================================
CREATE OR REPLACE FUNCTION update_updated_at_column() RETURNS TRIGGER AS $$ BEGIN NEW.updated_at = NOW();
RETURN NEW;
END;
$$ language 'plpgsql';
-- Apply trigger to credentials table
DROP TRIGGER IF EXISTS update_user_mt5_credentials_updated_at ON user_mt5_credentials;
CREATE TRIGGER update_user_mt5_credentials_updated_at BEFORE
UPDATE ON user_mt5_credentials FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
-- Apply trigger to configs table
DROP TRIGGER IF EXISTS update_user_trading_configs_updated_at ON user_trading_configs;
CREATE TRIGGER update_user_trading_configs_updated_at BEFORE
UPDATE ON user_trading_configs FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
-- ============================================
-- 5. HELPER FUNCTION: Check if MT5 is available
-- ============================================
-- Returns true if the MT5 account is not already linked to another user
CREATE OR REPLACE FUNCTION is_mt5_available(
        p_mt5_login VARCHAR,
        p_mt5_server VARCHAR,
        p_user_id UUID DEFAULT NULL
    ) RETURNS BOOLEAN AS $$
DECLARE existing_user UUID;
BEGIN
SELECT user_id INTO existing_user
FROM user_mt5_credentials
WHERE mt5_login = p_mt5_login
    AND mt5_server = p_mt5_server;
-- Available if no one has it, or if the same user has it
RETURN existing_user IS NULL
OR existing_user = p_user_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
-- ============================================
-- USAGE NOTES
-- ============================================
-- 
-- 1. Run this entire script in Supabase SQL Editor
-- 
-- 2. Generate an encryption key for your .env:
--    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
--
-- 3. Add to .env:
--    MT5_ENCRYPTION_KEY=your-generated-key-here
--
-- 4. The password stored in mt5_password_encrypted is encrypted 
--    server-side using Fernet before being sent to Supabase