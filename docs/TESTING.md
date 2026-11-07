# Verification (C15)

Target runner is ubuntu-22.04, Python 3.10, no real API keys. CI runs compileall,
all offline unit/integration tests with ResourceWarning promoted to errors, CLI
entry points, tracked-secret pattern checks, and wheel asset inclusion.

Local Windows checks are preliminary evidence only. POSIX credential tests skip
on Windows and must run without skip in Linux CI. Network-disabled environments
may require permission to connect to loopback for Web tests; no external network
is used by those tests.

Commands:
```bash
python -m unittest discover -s tests/unit -v
python -m unittest discover -s tests/integration -v
python -W error::ResourceWarning -m unittest discover -s tests -v
python scripts/check_secrets.py
```

B owns SQLite lifecycle/migration and memory regressions. A owns planning,
delegation and task E2E tests. C owns path/SSRF/Git scope, Docker execution and
platform guard tests. These suites are absent in the initial remote. C15 remains
an integration scaffold until those tests and a Docker CI job are supplied; this
workflow does not claim Docker/security/full-platform acceptance.

The pattern check is a precaution, not proof that all possible secrets are absent.
Never add real keys to tracked files.
