CREATE TABLE delegations (
    id TEXT PRIMARY KEY REFERENCES tasks(id),
    issue_id TEXT NOT NULL REFERENCES tasks(id),
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    check_json TEXT NOT NULL,
    outcome_json TEXT
);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (4, '0004_delegations', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 4;
