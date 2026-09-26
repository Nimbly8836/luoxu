-- Mandatory storage migration. BACK UP and stop all old indexers, web servers
-- and semantic workers first. Run with psql -v ON_ERROR_STOP=1.
-- One transaction copies and verifies all rows before removing old tables.
BEGIN;
LOCK TABLE conversations IN ACCESS EXCLUSIVE MODE;
CREATE TABLE IF NOT EXISTS message_archives (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  telegram_peer_type text NOT NULL CHECK (telegram_peer_type IN ('channel', 'chat', 'user')),
  telegram_peer_id bigint NOT NULL,
  table_version integer NOT NULL DEFAULT 1,
  UNIQUE (telegram_peer_type, telegram_peer_id)
);
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS archive_id uuid;
INSERT INTO message_archives (telegram_peer_type, telegram_peer_id)
  SELECT DISTINCT telegram_peer_type, telegram_peer_id FROM conversations
  ON CONFLICT (telegram_peer_type, telegram_peer_id) DO NOTHING;
UPDATE conversations c SET archive_id = a.id FROM message_archives a
  WHERE a.telegram_peer_type = c.telegram_peer_type AND a.telegram_peer_id = c.telegram_peer_id
    AND c.archive_id IS NULL;
ALTER TABLE conversations ALTER COLUMN archive_id SET NOT NULL;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='conversations'::regclass
                 AND conname='conversations_archive_id_fkey') THEN
    ALTER TABLE conversations ADD CONSTRAINT conversations_archive_id_fkey
      FOREIGN KEY (archive_id) REFERENCES message_archives(id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='conversations'::regclass
                 AND conname='conversations_id_archive_id_key') THEN
    ALTER TABLE conversations ADD CONSTRAINT conversations_id_archive_id_key UNIQUE (id, archive_id);
  END IF;
END $$;

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


DO $migration$
DECLARE
  a record;
  mt text;
  et text;
  cols text := 'conversation_id, group_id, msgid, reply_to_id, topic_id, quote_text, from_user, from_user_name, text, media, created_at, updated_at, deleted_at';
  ecols text := 'conversation_id, msgid, created_at, model, content_hash, embedding';
  mismatch boolean;
  has_vectors boolean;
BEGIN
  IF EXISTS (SELECT 1 FROM bootstrap_state WHERE name='per-peer-storage-v1') THEN
    RETURN;
  END IF;
  IF to_regclass('messages') IS NULL THEN
    RAISE EXCEPTION 'legacy messages table missing; refusing an unverifiable migration';
  END IF;
  LOCK TABLE messages IN ACCESS EXCLUSIVE MODE;
  has_vectors := to_regclass('message_embeddings') IS NOT NULL;
  IF has_vectors THEN
    LOCK TABLE message_embeddings IN ACCESS EXCLUSIVE MODE;
    -- LIKE does not copy foreign keys. pgvector is already installed here.
    EXECUTE 'CREATE TABLE IF NOT EXISTS message_embedding_template (LIKE message_embeddings INCLUDING ALL)';
  END IF;
  FOR a IN SELECT * FROM message_archives ORDER BY id LOOP
    PERFORM provision_message_archive(a.id);
    mt := 'messages_' || replace(a.id::text, '-', '');
    et := 'embeddings_' || replace(a.id::text, '-', '');
    EXECUTE format('INSERT INTO %I (%s) SELECT %s FROM messages
      WHERE conversation_id IN (SELECT id FROM conversations WHERE archive_id=$1)
      ON CONFLICT DO NOTHING', mt, cols, cols) USING a.id;
    -- Compare complete rows in BOTH directions (including text, edits, deletes).
    EXECUTE format('SELECT EXISTS (
      (SELECT %s FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE archive_id=$1)
       EXCEPT SELECT %s FROM %I)
      UNION ALL
      (SELECT %s FROM %I EXCEPT SELECT %s FROM messages
       WHERE conversation_id IN (SELECT id FROM conversations WHERE archive_id=$1)))',
      cols, cols, mt, cols, mt, cols) INTO mismatch USING a.id;
    IF mismatch THEN RAISE EXCEPTION 'message verification failed for %', a.id; END IF;
    IF has_vectors THEN
      EXECUTE format('INSERT INTO %I (%s) SELECT %s FROM message_embeddings
        WHERE conversation_id IN (SELECT id FROM conversations WHERE archive_id=$1)
        ON CONFLICT DO NOTHING', et, ecols, ecols) USING a.id;
      EXECUTE format('SELECT EXISTS (
        (SELECT %s FROM message_embeddings WHERE conversation_id IN (SELECT id FROM conversations WHERE archive_id=$1)
         EXCEPT SELECT %s FROM %I)
        UNION ALL
        (SELECT %s FROM %I EXCEPT SELECT %s FROM message_embeddings
         WHERE conversation_id IN (SELECT id FROM conversations WHERE archive_id=$1)))',
        ecols, ecols, et, ecols, et, ecols) INTO mismatch USING a.id;
      IF mismatch THEN RAISE EXCEPTION 'embedding verification failed for %', a.id; END IF;
    END IF;
  END LOOP;
  -- No CASCADE: unknown external dependencies must abort rather than be deleted.
  IF has_vectors THEN DROP TABLE message_embeddings; END IF;
  DROP TABLE messages;
  INSERT INTO bootstrap_state (name) VALUES ('per-peer-storage-v1');
END;
$migration$;
DROP FUNCTION IF EXISTS init_message_partitions();
DROP FUNCTION IF EXISTS create_messages_partition(integer);
COMMIT;
