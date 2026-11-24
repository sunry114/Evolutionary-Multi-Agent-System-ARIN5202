CREATE TABLE imported_project_chats (project_id TEXT PRIMARY KEY);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (13, '0013_shared_chats', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
PRAGMA user_version = 13;
