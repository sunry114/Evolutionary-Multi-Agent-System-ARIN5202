-- Revision links live in reviews. Each revision runs its own task tree.
UPDATE tasks AS revision
SET parent_id = NULL,
    owner = CASE WHEN revision.status = 'created' THEN NULL ELSE revision.owner END,
    assignee_type = source.assignee_type,
    assignee_id = source.assignee_id,
    routing_mode = CASE WHEN source.assignee_type IS NOT NULL THEN source.routing_mode END
FROM reviews AS link JOIN tasks AS source ON source.id = link.task_id
WHERE revision.id = link.revision_task_id;

INSERT INTO events(task_id, event_type, actor, payload_json, created_at)
SELECT r.revision_task_id, 'issue_config', 'human',
       json_object('checks', json(COALESCE(
           (SELECT json_extract(e.payload_json, '$.checks') FROM events e
            WHERE e.task_id = r.task_id AND e.event_type IN ('issue_config', 'run_config', 'plan_created')
            ORDER BY e.id DESC LIMIT 1), '{}')),
           'memory_enabled', json(COALESCE(
           (SELECT CASE json_extract(e.payload_json, '$.memory_enabled') WHEN 0 THEN 'false' ELSE 'true' END
            FROM events e WHERE e.task_id = r.task_id AND e.event_type IN ('issue_config', 'run_config')
            ORDER BY e.id DESC LIMIT 1), 'true'))),
       t.created_at
FROM reviews r JOIN tasks t ON t.id = r.revision_task_id
WHERE NOT EXISTS (
    SELECT 1 FROM events e WHERE e.task_id = t.id AND e.event_type = 'issue_config'
);

UPDATE events SET payload_json = json_remove(payload_json, '$.group_context')
WHERE event_type = 'issue_config';

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (5, '0005_workbench', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 5;
