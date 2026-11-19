CREATE TABLE group_chats (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE group_chat_members (
    group_id TEXT NOT NULL REFERENCES group_chats(id) ON DELETE CASCADE,
    agent TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (group_id, agent),
    UNIQUE (group_id, position)
);

ALTER TABLE messages ADD COLUMN group_id TEXT REFERENCES group_chats(id)
    CHECK (group_id IS NULL OR (kind = 'group' AND task_id IS NULL AND chat_id IS NULL AND recipient IS NULL));

CREATE INDEX idx_messages_group ON messages(group_id, created_at, id) WHERE group_id IS NOT NULL;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (9, '0009_group_chats', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
PRAGMA user_version = 9;
