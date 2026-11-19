ALTER TABLE tasks ADD COLUMN routing_mode TEXT
    CHECK (routing_mode IN ('squad', 'bidding', 'direct'));

UPDATE tasks
SET routing_mode = CASE
    WHEN assignee_type = 'agent' AND status = 'created' THEN 'direct'
    ELSE 'bidding'
END
WHERE assignee_type IS NOT NULL OR EXISTS (
    SELECT 1 FROM events WHERE events.task_id = tasks.id AND event_type = 'run_config'
);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (3, '0003_routing', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 3;
