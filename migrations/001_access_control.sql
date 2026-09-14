-- Authentication and authorization tables. Run once on an existing database.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS auth_users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  username text NOT NULL UNIQUE CHECK (username ~ '^[A-Za-z0-9_.-]{1,64}$'),
  password_hash text NOT NULL,
  is_admin boolean NOT NULL DEFAULT false,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS auth_refresh_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
  token_hash text NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS auth_refresh_active_idx
  ON auth_refresh_tokens (token_hash, expires_at) WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS conversation_access (
  user_id uuid NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
  conversation_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS public_conversation_access (
  conversation_id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bootstrap_state (
  name text PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now()
);
