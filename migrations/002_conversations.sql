-- Add UUID conversations while preserving Telegram group IDs and old endpoints.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind text NOT NULL CHECK (kind IN ('group', 'topic', 'private_chat')),
  telegram_peer_type text NOT NULL DEFAULT 'channel'
    CHECK (telegram_peer_type IN ('channel', 'chat', 'user')),
  telegram_peer_id bigint NOT NULL,
  topic_id bigint,
  name text NOT NULL,
  pub_id text,
  legacy_group_id bigint,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((kind = 'private_chat' AND telegram_peer_type = 'user' AND topic_id IS NULL)
      OR (kind IN ('group', 'topic') AND telegram_peer_type IN ('channel', 'chat')))
);
CREATE UNIQUE INDEX IF NOT EXISTS conversations_telegram_idx
  ON conversations (kind, telegram_peer_type, telegram_peer_id, coalesce(topic_id, 0));
CREATE INDEX IF NOT EXISTS conversations_kind_idx ON conversations (kind);
CREATE INDEX IF NOT EXISTS conversations_legacy_group_idx ON conversations (legacy_group_id)
  WHERE legacy_group_id IS NOT NULL;

ALTER TABLE tg_groups ADD COLUMN IF NOT EXISTS conversation_id uuid;
INSERT INTO conversations (kind, telegram_peer_type, telegram_peer_id, name, pub_id, legacy_group_id)
SELECT 'group', 'channel', g.group_id, g.name, g.pub_id, g.group_id
FROM tg_groups g
WHERE NOT EXISTS (SELECT 1 FROM conversations c WHERE c.legacy_group_id = g.group_id);
UPDATE tg_groups g SET conversation_id = c.id
FROM conversations c WHERE c.legacy_group_id = g.group_id AND g.conversation_id IS NULL;
ALTER TABLE tg_groups ALTER COLUMN conversation_id SET NOT NULL;
ALTER TABLE tg_groups DROP CONSTRAINT IF EXISTS tg_groups_conversation_id_fkey;
ALTER TABLE tg_groups ADD CONSTRAINT tg_groups_conversation_id_fkey
  FOREIGN KEY (conversation_id) REFERENCES conversations(id);
CREATE UNIQUE INDEX IF NOT EXISTS tg_groups_conversation_id_idx
  ON tg_groups(conversation_id);

ALTER TABLE conversation_access
  DROP CONSTRAINT IF EXISTS conversation_access_conversation_id_fkey;
ALTER TABLE conversation_access
  ADD CONSTRAINT conversation_access_conversation_id_fkey
  FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE;
ALTER TABLE public_conversation_access
  DROP CONSTRAINT IF EXISTS public_conversation_access_conversation_id_fkey;
ALTER TABLE public_conversation_access
  ADD CONSTRAINT public_conversation_access_conversation_id_fkey
  FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE;
