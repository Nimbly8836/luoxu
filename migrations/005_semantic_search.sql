-- OPTIONAL: install pgvector first. On legacy databases apply 006 BEFORE 005.
-- Fresh dbsetup.sql already has the per-group storage layout.
BEGIN;
DO $$ BEGIN
  IF to_regclass('message_archives') IS NULL THEN
    RAISE EXCEPTION 'apply 006_per_group_storage.sql before enabling semantic search';
  END IF;
END $$;
-- Serialize provisioning against concurrent creation of new archives.
LOCK TABLE message_archives IN EXCLUSIVE MODE;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS message_embedding_template (
  conversation_id uuid NOT NULL,
  msgid bigint NOT NULL,
  created_at timestamptz NOT NULL,
  model text NOT NULL,
  content_hash text NOT NULL,
  embedding vector(512) NOT NULL,
  PRIMARY KEY (conversation_id, msgid, created_at, model)
);
DO $$ DECLARE a record; BEGIN
  FOR a IN SELECT id FROM message_archives ORDER BY id LOOP
    PERFORM provision_message_archive(a.id);
  END LOOP;
END $$;
COMMIT;
