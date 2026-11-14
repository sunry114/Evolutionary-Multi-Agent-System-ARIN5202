
PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL
);
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    owner TEXT,
    parent_id TEXT REFERENCES tasks(id),
    status TEXT NOT NULL CHECK (
        status IN (
            'created',
            'planning',
            'ready',
            'assigned',
            'running',
            'waiting_human',
            'success',
            'failed',
            'blocked'
        )
    ),
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_tasks_parent_id
    ON tasks(parent_id);

CREATE INDEX idx_tasks_status
    ON tasks(status);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT,
    task_id TEXT REFERENCES tasks(id),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_messages_task_id
    ON messages(task_id);

CREATE INDEX idx_messages_kind
    ON messages(kind);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT REFERENCES tasks(id),
    event_type TEXT NOT NULL,
    actor TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_events_task_id_id
    ON events(task_id, id);

CREATE TABLE actions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    agent TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (
        status IN ('requested', 'running', 'success', 'failed', 'blocked')
    ),
    result_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX idx_actions_task_id
    ON actions(task_id);

CREATE TABLE approval_requests (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    agent TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('approval', 'clarification')
    ),
    action TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'approved', 'denied', 'answered', 'expired')
    ),
    created_at TEXT NOT NULL,
    answered_at TEXT,
    answer_json TEXT
);

CREATE INDEX idx_approval_requests_task_id
    ON approval_requests(task_id);

CREATE INDEX idx_approval_requests_status
    ON approval_requests(status);

CREATE TABLE reviews (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'accepted', 'changes_requested', 'revising')
    ),
    feedback TEXT NOT NULL DEFAULT '',
    revision_task_id TEXT UNIQUE REFERENCES tasks(id),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    updated_at TEXT NOT NULL
);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (
    1,
    '0001_initial',
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
);

PRAGMA user_version = 1;

COMMIT;
