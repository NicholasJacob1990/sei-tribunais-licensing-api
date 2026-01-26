-- Migration: Add API token fields for MCP client authentication
-- Date: 2026-01-26

-- Add api_token_hash column (stores SHA256 hash of the token)
ALTER TABLE users ADD COLUMN IF NOT EXISTS api_token_hash VARCHAR(255);

-- Add api_token_created_at column
ALTER TABLE users ADD COLUMN IF NOT EXISTS api_token_created_at TIMESTAMP WITH TIME ZONE;

-- Create index for fast token lookup
CREATE INDEX IF NOT EXISTS idx_users_api_token_hash ON users(api_token_hash);
