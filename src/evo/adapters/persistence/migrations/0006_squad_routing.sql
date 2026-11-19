UPDATE events AS e
SET payload_json = json_set(
    e.payload_json, '$.routing_mode', 'direct', '$.squad', json('null'),
    '$.direct_assignee', (SELECT assignee_id FROM tasks WHERE id = e.task_id))
WHERE e.event_type = 'run_config' AND json_extract(e.payload_json, '$.routing_mode') = 'bidding'
  AND (SELECT assignee_type FROM tasks WHERE id = e.task_id) = 'agent';

UPDATE events AS e
SET payload_json = json_set(e.payload_json, '$.routing_mode', 'squad', '$.squad', json(
    COALESCE(json_extract(e.payload_json, '$.squad'), json_object(
        'name', COALESCE((SELECT assignee_id FROM tasks WHERE id = e.task_id), 'default'),
        'lead', json_extract(e.payload_json, '$.agents[0].name'),
        'members', (SELECT json_group_array(json_extract(agent.value, '$.name'))
                    FROM json_each(e.payload_json, '$.agents') AS agent)
    ))
))
WHERE e.event_type = 'run_config' AND json_extract(e.payload_json, '$.routing_mode') = 'bidding';

UPDATE events AS e
SET payload_json = json_set(e.payload_json, '$.goal', (SELECT description FROM tasks WHERE id = e.task_id))
WHERE e.event_type = 'plan_created' AND json_type(e.payload_json, '$.goal') IS NULL;

ALTER TABLE tasks RENAME COLUMN routing_mode TO old_routing_mode;
ALTER TABLE tasks ADD COLUMN routing_mode TEXT CHECK (routing_mode IN ('squad', 'direct'));

UPDATE tasks
SET routing_mode = CASE
    WHEN old_routing_mode = 'bidding' AND assignee_type = 'agent' THEN 'direct'
    WHEN old_routing_mode = 'bidding' THEN 'squad'
    ELSE old_routing_mode
END
WHERE old_routing_mode IS NOT NULL;

ALTER TABLE tasks DROP COLUMN old_routing_mode;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (6, '0006_squad_routing', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 6;
