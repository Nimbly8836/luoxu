-- Move messages onto conversations and add reply, deletion, and history fields.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS conversation_id uuid;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS reply_to_id bigint;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS topic_id bigint;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS quote_text text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS media jsonb;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

UPDATE messages m SET conversation_id = g.conversation_id
FROM tg_groups g WHERE m.group_id = g.group_id AND m.conversation_id IS NULL;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM messages WHERE conversation_id IS NULL) THEN
    RAISE EXCEPTION 'messages without a matching tg_groups conversation remain';
  END IF;
END $$;
ALTER TABLE messages ALTER COLUMN conversation_id SET NOT NULL;
ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_conversation_id_fkey;
ALTER TABLE messages ADD CONSTRAINT messages_conversation_id_fkey
  FOREIGN KEY (conversation_id) REFERENCES conversations(id);
ALTER TABLE messages ALTER COLUMN group_id DROP NOT NULL;

DROP INDEX IF EXISTS messages_msgid_idx;
CREATE UNIQUE INDEX IF NOT EXISTS messages_conversation_msgid_idx
  ON messages (conversation_id, msgid, created_at DESC);
CREATE INDEX IF NOT EXISTS messages_conversation_time_idx
  ON messages (conversation_id, created_at DESC, msgid DESC);
CREATE INDEX IF NOT EXISTS messages_reply_idx ON messages (conversation_id, reply_to_id);

CREATE TABLE IF NOT EXISTS message_revisions (
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
ALTER TABLE message_revisions ADD COLUMN IF NOT EXISTS quote_text text;
ALTER TABLE message_revisions ADD COLUMN IF NOT EXISTS created_at timestamptz;
ALTER TABLE message_revisions ADD COLUMN IF NOT EXISTS updated_at timestamptz;
ALTER TABLE message_revisions ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
UPDATE message_revisions SET created_at = captured_at WHERE created_at IS NULL;
ALTER TABLE message_revisions ALTER COLUMN created_at SET NOT NULL;
CREATE INDEX IF NOT EXISTS message_revisions_lookup_idx
  ON message_revisions (conversation_id, msgid, captured_at DESC);

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
