CREATE TABLE chat_threads (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_chat_threads_default ON chat_threads(agent) WHERE is_default = 1;
ALTER TABLE messages ADD COLUMN chat_id TEXT REFERENCES chat_threads(id)
    CHECK (chat_id IS NULL OR (kind = 'dm' AND task_id IS NULL));
CREATE INDEX idx_messages_chat ON messages(chat_id, created_at, id) WHERE chat_id IS NOT NULL;

INSERT INTO chat_threads(id, agent, is_default, created_at, updated_at)
SELECT lower(hex(randomblob(16))), agent, 1, MIN(created_at), MAX(created_at)
FROM (
    SELECT CASE WHEN sender = 'human' THEN recipient ELSE sender END AS agent, created_at
    FROM messages WHERE kind = 'dm' AND task_id IS NULL AND (
        (sender = 'human' AND recipient IS NOT NULL AND recipient <> 'human')
        OR (recipient = 'human' AND sender <> 'human')
    )
) GROUP BY agent;

UPDATE messages AS m SET chat_id = (
    SELECT c.id FROM chat_threads c WHERE c.is_default = 1 AND (
        (m.sender = 'human' AND m.recipient = c.agent) OR (m.recipient = 'human' AND m.sender = c.agent)
    )
) WHERE kind = 'dm' AND task_id IS NULL;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (8, '0008_chat_threads', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
PRAGMA user_version = 8;
