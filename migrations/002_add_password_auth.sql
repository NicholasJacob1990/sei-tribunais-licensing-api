-- Migration: Add email/password authentication support
-- Date: 2026-01-25
-- Description: Adds password_hash column and makes google_id optional

-- Add password_hash column for email/password users
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);

-- Make google_id nullable (for email/password users)
ALTER TABLE users ALTER COLUMN google_id DROP NOT NULL;

-- Comment
COMMENT ON COLUMN users.password_hash IS 'Bcrypt hash for email/password authentication';
