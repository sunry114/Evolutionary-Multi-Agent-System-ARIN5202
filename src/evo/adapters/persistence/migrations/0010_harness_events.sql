CREATE TABLE harness_events (
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence)
) WITHOUT ROWID;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (10, '0010_harness_events', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
PRAGMA user_version = 10;
