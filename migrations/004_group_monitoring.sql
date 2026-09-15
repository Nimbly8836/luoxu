BEGIN;

CREATE TABLE IF NOT EXISTS group_monitoring (
    conversation_id uuid PRIMARY KEY
    REFERENCES conversations (id) ON DELETE CASCADE,
    manual_enabled boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Operator-approved ONE-TIME adoption of existing group registrations.
-- Old releases cannot distinguish dynamic monitoring from archive-only groups.
-- This preserves collection, not read access: no archive is published/granted.
-- A repeat must never re-enable tombstones or adopt later archive-only groups.
DO $$
BEGIN
    INSERT INTO bootstrap_state (name)
    VALUES ('group-monitoring-legacy-v1')
    ON CONFLICT (name) DO NOTHING;
    IF FOUND THEN
        INSERT INTO group_monitoring (conversation_id)
        SELECT g.conversation_id FROM tg_groups AS g
        INNER JOIN conversations AS c ON g.conversation_id = c.id
        WHERE c.kind = 'group'
        ON CONFLICT (conversation_id) DO NOTHING;
    END IF;
END;
$$;

COMMIT;
