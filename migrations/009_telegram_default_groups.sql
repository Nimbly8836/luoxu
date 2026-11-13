-- Per-Telegram-user defaults and single-consumer update checkpoints.
-- Does not add archive grants or monitoring references.
BEGIN;
CREATE TABLE IF NOT EXISTS telegram_default_groups (
  telegram_id bigint PRIMARY KEY CHECK (telegram_id > 0 AND telegram_id < 4503599627370496),
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS telegram_bot_offsets (
  bot_id bigint PRIMARY KEY CHECK (bot_id > 0),
  next_update_id bigint NOT NULL DEFAULT 0 CHECK (next_update_id >= 0),
  updated_at timestamptz NOT NULL DEFAULT now()
);
COMMIT;
