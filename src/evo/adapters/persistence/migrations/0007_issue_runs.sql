ALTER TABLE tasks ADD COLUMN issue_status TEXT CHECK (
    issue_status IN ('backlog', 'todo', 'in_progress', 'in_review', 'blocked', 'done', 'cancelled')
);

WITH RECURSIVE revisions(issue_id, task_id) AS (
    SELECT t.id, t.id FROM tasks t
    WHERE t.parent_id IS NULL AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.revision_task_id=t.id)
    UNION ALL
    SELECT revisions.issue_id, r.revision_task_id FROM revisions JOIN reviews r ON r.task_id=revisions.task_id
    WHERE r.revision_task_id IS NOT NULL
), latest AS (
    SELECT revisions.issue_id, t.status, r.status AS review_status FROM revisions
    JOIN tasks t ON t.id=revisions.task_id LEFT JOIN reviews r ON r.task_id=t.id
    WHERE r.revision_task_id IS NULL
)
UPDATE tasks SET issue_status=(SELECT CASE
    WHEN latest.status='success' AND latest.review_status='accepted' THEN 'done'
    WHEN latest.status='success' THEN 'in_review'
    WHEN latest.status='created' THEN 'todo'
    ELSE 'blocked'
END FROM latest WHERE latest.issue_id=tasks.id)
WHERE id IN (SELECT issue_id FROM latest);

CREATE TABLE issue_runs (
    id TEXT PRIMARY KEY REFERENCES tasks(id),
    issue_id TEXT NOT NULL REFERENCES tasks(id),
    agent TEXT NOT NULL,
    agent_role TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('assignment', 'mention', 'handoff')),
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'stopping', 'completed', 'failed', 'stopped', 'interrupted'
    )),
    comment_id TEXT REFERENCES messages(id),
    parent_run_id TEXT REFERENCES issue_runs(id),
    retry_of TEXT UNIQUE REFERENCES issue_runs(id),
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX idx_issue_runs_issue ON issue_runs(issue_id, created_at);
CREATE INDEX idx_issue_runs_queue ON issue_runs(status, created_at);
CREATE UNIQUE INDEX idx_issue_runs_comment_target ON issue_runs(comment_id, agent)
    WHERE comment_id IS NOT NULL AND retry_of IS NULL;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (7, '0007_issue_runs', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
PRAGMA user_version = 7;
