ALTER TABLE tasks ADD COLUMN assignee_type TEXT
    CHECK (assignee_type IN ('agent', 'squad'));
ALTER TABLE tasks ADD COLUMN assignee_id TEXT CHECK (
    (assignee_type IS NULL AND assignee_id IS NULL)
    OR (parent_id IS NULL AND assignee_type IS NOT NULL AND assignee_id IS NOT NULL
        AND length(trim(assignee_id)) > 0)
);

UPDATE events
SET payload_json = json_set(payload_json, '$.squad', json('null'), '$.routing_mode', 'bidding')
WHERE event_type = 'run_config';

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (2, '0002_issues', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 2;
