CREATE EXTENSION IF NOT EXISTS pgroonga;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE message_archives (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  telegram_peer_type text NOT NULL CHECK (telegram_peer_type IN ('channel', 'chat', 'user')),
  telegram_peer_id bigint NOT NULL,
  table_version integer NOT NULL DEFAULT 1,
  UNIQUE (telegram_peer_type, telegram_peer_id)
);

CREATE TABLE conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  archive_id uuid NOT NULL REFERENCES message_archives(id),
  UNIQUE (id, archive_id),
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
CREATE TABLE auth_telegram_launches (
  fingerprint text PRIMARY KEY,
  expires_at timestamptz NOT NULL
);
CREATE INDEX auth_telegram_launches_expiry_idx ON auth_telegram_launches (expires_at);
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

-- Existing installations: apply outstanding 001-004, then 006_per_group_storage.
-- Optional semantic search: apply 005 after 006 (also on fresh databases).

-- Independent ordinary tables, shared DDL template (never used for data).
CREATE TABLE IF NOT EXISTS message_template (
  archive_id uuid NOT NULL,
  conversation_id uuid NOT NULL,
  group_id bigint,
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
);
CREATE INDEX IF NOT EXISTS message_template_text_idx ON message_template USING pgroonga (text)
  WITH (tokenizer='TokenNgram("report_source_location", true, "loose_blank", true)');
CREATE INDEX IF NOT EXISTS message_template_time_idx ON message_template (conversation_id, created_at DESC, msgid DESC);
CREATE INDEX IF NOT EXISTS message_template_reply_idx ON message_template (conversation_id, reply_to_id);
CREATE INDEX IF NOT EXISTS message_template_sender_idx ON message_template (from_user, created_at DESC);

-- Small permission-supporting index for /avatar/{uid}; no text or names here.
CREATE TABLE IF NOT EXISTS archive_senders (
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  uid bigint NOT NULL,
  live_messages bigint NOT NULL CHECK (live_messages >= 0),
  PRIMARY KEY (conversation_id, uid)
);
CREATE INDEX IF NOT EXISTS archive_senders_uid_idx ON archive_senders (uid);

CREATE OR REPLACE FUNCTION update_archive_senders()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND OLD.conversation_id = NEW.conversation_id
     AND OLD.from_user IS NOT DISTINCT FROM NEW.from_user
     AND (OLD.deleted_at IS NULL) = (NEW.deleted_at IS NULL) THEN
    RETURN NULL;
  END IF;
  IF TG_OP <> 'INSERT' AND OLD.from_user IS NOT NULL AND OLD.deleted_at IS NULL THEN
    UPDATE archive_senders SET live_messages = live_messages - 1
      WHERE conversation_id = OLD.conversation_id AND uid = OLD.from_user;
  END IF;
  IF TG_OP <> 'DELETE' AND NEW.from_user IS NOT NULL AND NEW.deleted_at IS NULL THEN
    INSERT INTO archive_senders (conversation_id, uid, live_messages)
      VALUES (NEW.conversation_id, NEW.from_user, 1)
      ON CONFLICT (conversation_id, uid) DO UPDATE
        SET live_messages = archive_senders.live_messages + 1;
  END IF;
  RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION invalidate_archive_embedding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE format('DELETE FROM %I.%I WHERE conversation_id=$1 AND msgid=$2 AND created_at=$3',
    TG_TABLE_SCHEMA, 'embeddings_' || replace(OLD.archive_id::text, '-', ''))
    USING OLD.conversation_id, OLD.msgid, OLD.created_at;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION provision_message_archive(aid uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  mt text := 'messages_' || replace(aid::text, '-', '');
  et text := 'embeddings_' || replace(aid::text, '-', '');
  ns text := current_schema();
  version int;
BEGIN
  SELECT table_version INTO version FROM message_archives WHERE id=aid FOR UPDATE;
  IF version IS DISTINCT FROM 1 THEN
    RAISE EXCEPTION 'unknown archive or unsupported table version: %', aid;
  END IF;
  IF to_regclass(format('%I.%I', ns, mt)) IS NULL THEN
    EXECUTE format('CREATE TABLE %I.%I (LIKE %I.message_template INCLUDING ALL)', ns, mt, ns);
    EXECUTE format('ALTER TABLE %I.%I ALTER COLUMN archive_id SET DEFAULT %L::uuid', ns, mt, aid);
    EXECUTE format('ALTER TABLE %I.%I ADD CHECK (archive_id = %L::uuid)', ns, mt, aid);
    EXECUTE format('ALTER TABLE %I.%I ADD FOREIGN KEY (conversation_id, archive_id)
      REFERENCES conversations(id, archive_id)', ns, mt);
    EXECUTE format('ALTER TABLE %I.%I ADD FOREIGN KEY (group_id) REFERENCES tg_groups(group_id)', ns, mt);
    EXECUTE format('CREATE TRIGGER archive_sender_changed AFTER INSERT OR UPDATE OR DELETE ON %I.%I
      FOR EACH ROW EXECUTE FUNCTION update_archive_senders()', ns, mt);
  END IF;
  IF to_regclass(format('%I.message_embedding_template', ns)) IS NOT NULL
     AND to_regclass(format('%I.%I', ns, et)) IS NULL THEN
    EXECUTE format('CREATE TABLE %I.%I (LIKE %I.message_embedding_template INCLUDING ALL)', ns, et, ns);
    EXECUTE format('ALTER TABLE %I.%I ADD FOREIGN KEY (conversation_id, msgid, created_at)
      REFERENCES %I.%I (conversation_id, msgid, created_at) ON UPDATE CASCADE ON DELETE CASCADE', ns, et, ns, mt);
    EXECUTE format('CREATE TRIGGER archive_embedding_invalidated BEFORE UPDATE ON %I.%I
      FOR EACH ROW WHEN (OLD.text IS DISTINCT FROM NEW.text
        OR OLD.deleted_at IS DISTINCT FROM NEW.deleted_at
        OR OLD.conversation_id IS DISTINCT FROM NEW.conversation_id
        OR OLD.msgid IS DISTINCT FROM NEW.msgid OR OLD.created_at IS DISTINCT FROM NEW.created_at)
      EXECUTE FUNCTION invalidate_archive_embedding()', ns, mt);
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ensure_message_archive(pt text, pid bigint)
RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE aid uuid;
BEGIN
  INSERT INTO message_archives (telegram_peer_type, telegram_peer_id)
    VALUES (pt, pid) ON CONFLICT (telegram_peer_type, telegram_peer_id) DO NOTHING;
  SELECT id INTO aid FROM message_archives WHERE telegram_peer_type=pt AND telegram_peer_id=pid;
  PERFORM provision_message_archive(aid);
  RETURN aid;
END;
$$;

INSERT INTO bootstrap_state (name) VALUES ('per-peer-storage-v1');
