-- Mini App visitors have independent local identities, never automatic grants.
BEGIN;
CREATE TABLE IF NOT EXISTS auth_telegram_accounts (
  telegram_id bigint PRIMARY KEY CHECK (telegram_id > 0 AND telegram_id < 4503599627370496),
  user_id uuid NOT NULL UNIQUE REFERENCES auth_users(id) ON DELETE CASCADE
);
COMMIT;
