ALTER TABLE tasks ADD COLUMN title TEXT;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (11, '0011_issue_titles', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 11;
