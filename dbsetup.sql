CREATE EXTENSION IF NOT EXISTS pgroonga;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind text NOT NULL CHECK (kind IN ('group', 'topic', 'private_chat')),
  telegram_peer_type text NOT NULL CHECK (telegram_peer_type IN ('channel', 'chat', 'user')),
  telegram_peer_id bigint NOT NULL,
  topic_id bigint,
  name text NOT NULL,
  pub_id text,
  legacy_group_id bigint,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((kind = 'private_chat' AND telegram_peer_type = 'user' AND topic_id IS NULL)
      OR (kind IN ('group', 'topic') AND telegram_peer_type IN ('channel', 'chat')))
);
CREATE UNIQUE INDEX conversations_telegram_idx
  ON conversations (kind, telegram_peer_type, telegram_peer_id, coalesce(topic_id, 0));
CREATE INDEX conversations_kind_idx ON conversations (kind);
CREATE INDEX conversations_legacy_group_idx ON conversations (legacy_group_id)
  WHERE legacy_group_id IS NOT NULL;

-- Compatibility table for the original /groups and /search?g= APIs.
CREATE TABLE tg_groups (
  group_id bigint PRIMARY KEY,
  name text NOT NULL,
  pub_id text,
  loaded_first_id bigint,
  loaded_last_id bigint,
  conversation_id uuid NOT NULL UNIQUE REFERENCES conversations(id)
);

CREATE TABLE messages (
  conversation_id uuid NOT NULL REFERENCES conversations(id),
  group_id bigint REFERENCES tg_groups(group_id),
  msgid bigint NOT NULL,
  reply_to_id bigint,
  topic_id bigint,
  quote_text text,
  from_user bigint,
  from_user_name text NOT NULL,
  text text NOT NULL,
  media jsonb,
  created_at timestamptz NOT NULL,
  updated_at timestamptz,
  deleted_at timestamptz,
  PRIMARY KEY (conversation_id, msgid, created_at)
) PARTITION BY RANGE (created_at);

CREATE OR REPLACE FUNCTION create_messages_partition(year int)
RETURNS void AS $$
BEGIN
  EXECUTE format(
    'CREATE TABLE IF NOT EXISTS messages_y%s PARTITION OF messages FOR VALUES FROM (%L) TO (%L)',
    year, format('%s-01-01', year), format('%s-01-01', year + 1)
  );
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION init_message_partitions()
RETURNS void AS $$
DECLARE
  start_year int := 2016;
  current_year int := extract(year FROM current_date);
  i int;
BEGIN
  FOR i IN start_year..current_year + 1 LOOP
    PERFORM create_messages_partition(i);
  END LOOP;
END;
$$ LANGUAGE plpgsql;
SELECT init_message_partitions();

CREATE INDEX message_idx ON messages USING pgroonga (text)
  WITH (tokenizer='TokenNgram("report_source_location", true, "loose_blank", true)');
CREATE INDEX messages_conversation_time_idx ON messages (conversation_id, created_at DESC, msgid DESC);
CREATE INDEX messages_reply_idx ON messages (conversation_id, reply_to_id);
CREATE INDEX messages_sender_idx ON messages (from_user, created_at DESC);

CREATE TABLE usernames (
  name text NOT NULL,
  uid bigint[] NOT NULL,
  group_id bigint[] NOT NULL DEFAULT '{}',
  last_seen timestamptz NOT NULL
);
CREATE UNIQUE INDEX usernames_uidx ON usernames (name);
CREATE INDEX usernames_idx ON usernames USING pgroonga (name)
  WITH (tokenizer='TokenBigramSplitSymbolAlphaDigit');

CREATE OR REPLACE FUNCTION array_distinct(anyarray) RETURNS anyarray AS $f$
  SELECT array_agg(DISTINCT x) FROM unnest($1) t(x);
$f$ LANGUAGE SQL IMMUTABLE;

CREATE OR REPLACE FUNCTION update_usernames()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.group_id IS NULL OR NEW.from_user IS NULL OR NEW.deleted_at IS NOT NULL THEN
    RETURN NEW;
  END IF;
  INSERT INTO usernames (name, uid, group_id, last_seen)
    VALUES (NEW.from_user_name, ARRAY[NEW.from_user], ARRAY[NEW.group_id], NEW.created_at)
    ON CONFLICT (name) DO UPDATE
      SET last_seen = CASE WHEN usernames.last_seen > NEW.created_at
                           THEN usernames.last_seen ELSE NEW.created_at END,
          uid = array_distinct(usernames.uid || NEW.from_user),
          group_id = array_distinct(usernames.group_id || NEW.group_id);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER table_updated AFTER INSERT ON messages
  FOR EACH ROW EXECUTE PROCEDURE update_usernames();

CREATE TABLE auth_users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  username text NOT NULL UNIQUE CHECK (username ~ '^[A-Za-z0-9_.-]{1,64}$'),
  password_hash text NOT NULL,
  is_admin boolean NOT NULL DEFAULT false,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE auth_refresh_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
  token_hash text NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX auth_refresh_active_idx ON auth_refresh_tokens (token_hash, expires_at)
  WHERE revoked_at IS NULL;
CREATE TABLE conversation_access (
  user_id uuid NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, conversation_id)
);
CREATE TABLE public_conversation_access (
  conversation_id uuid PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE bootstrap_state (
  name text PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Manual indexing references, independent of public/user read permissions.
-- Keep disabled rows as tombstones so configuration import cannot resurrect them.
CREATE TABLE group_monitoring (
  conversation_id uuid PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
  manual_enabled boolean NOT NULL DEFAULT true,
  updated_at timestamptz NOT NULL DEFAULT now()
);
-- Fresh installs have no legacy archives to adopt. Replaying upgrade 004 later
-- must not opt newer archive-only groups into monitoring.
INSERT INTO bootstrap_state (name) VALUES ('group-monitoring-legacy-v1');

CREATE TABLE message_revisions (
  id bigserial PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  msgid bigint NOT NULL,
  revision_type text NOT NULL CHECK (revision_type IN ('edit', 'delete')),
  text text NOT NULL,
  from_user bigint,
  from_user_name text NOT NULL,
  reply_to_id bigint,
  topic_id bigint,
  quote_text text,
  media jsonb,
  created_at timestamptz NOT NULL,
  updated_at timestamptz,
  deleted_at timestamptz,
  captured_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX message_revisions_lookup_idx
  ON message_revisions (conversation_id, msgid, captured_at DESC);

-- Existing installations must run migrations/001_access_control.sql,
-- migrations/002_conversations.sql, migrations/003_message_history.sql,
-- and migrations/004_group_monitoring.sql.
