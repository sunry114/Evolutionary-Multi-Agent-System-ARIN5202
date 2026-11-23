CREATE TABLE issue_run_triggers (
    run_id TEXT NOT NULL REFERENCES issue_runs(id),
    comment_id TEXT NOT NULL REFERENCES messages(id),
    parent_run_id TEXT REFERENCES issue_runs(id),
    PRIMARY KEY (run_id, comment_id)
);

INSERT INTO issue_run_triggers (run_id, comment_id, parent_run_id)
SELECT id, comment_id, parent_run_id FROM issue_runs WHERE comment_id IS NOT NULL;

CREATE INDEX idx_issue_run_triggers_parent ON issue_run_triggers(parent_run_id);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (12, '0012_issue_run_triggers', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
PRAGMA user_version = 12;
