-- Apply before enabling web.auth.telegram. No archive or grant changes.
BEGIN;
CREATE TABLE IF NOT EXISTS auth_telegram_launches (
  fingerprint text PRIMARY KEY,
  expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_telegram_launches_expiry_idx
  ON auth_telegram_launches (expires_at);
COMMIT;
