# Verification (C15)

Updated: 2026-11-13 (Asia/Shanghai). Current SQLite integration is based on main
cbac88e. Full local regression: 66 tests, 64 passed, 2 POSIX skips. Historical
API-only evidence below refers to 6cb3780.
The test suite is authored for this repository; it is not the instructor's
mid/final dataset. No ordinary test calls a real LLM.

## Current evidence

| Environment / revision | Result | Evidence |
|---|---|---|
| Windows, Python 3.12.7 / 6cb3780 | 38 tests: 36 passed, 2 POSIX tests skipped | Local unittest execution recorded in this development session |
| Ubuntu 22.04, Python 3.10 / 6cb3780 | CI succeeded, including offline tests and POSIX credential checks | [PR #2 run](https://github.com/sunry114/Evolutionary-Multi-Agent-System-ARIN5202/actions/runs/42449807107) |
| Ubuntu 22.04 / 08ed70d (historical) | Initial interface CI succeeded; initial suite had 22 tests | [PR #1 run](https://github.com/sunry114/Evolutionary-Multi-Agent-System-ARIN5202/actions/runs/42438183681) |

CI also checks compileall, CLI entry points, tracked-secret patterns, installation
and packaged Web asset inclusion. Live HKGAI connectivity is separate evidence:
[API_SMOKE_REPORT.md](API_SMOKE_REPORT.md). A successful smoke check is not a
task completion benchmark.

## Historical API milestone inventory (38 methods)

| File | Count | What it verifies |
|---|---:|---|
| tests/test_baseline.py | 2 | Installed version and CLI help |
| tests/unit/test_credentials.py | 3 | Environment credentials, POSIX file modes and symlink rejection |
| tests/unit/test_llm.py | 7 | Request format, retries, exhausted retries, safe failures, malformed completions, endpoint restrictions and redirects |
| tests/unit/test_provider_diagnostics.py | 16 | Environment config, shared provider factory, token validation, one bounded diagnostic request, safe reports, CLI flags/exits, hidden-key fallback refusal, TLS and transport boundaries |
| tests/integration/test_interfaces.py | 10 | CLI dispatch, missing backend, demo blocking, HTTP protections, redaction and review/revision contract |
| Total | 38 | Includes subcases within some test methods |

Web integration tests start a real local HTTP server and connect to loopback.
Their application backend is a demo/fixture. Provider tests use scripted responses.
The original ReviewTests use an in-memory repository fixture. New
test_sqlite_interfaces.py uses real SQLite, independent CLI processes and actual
Web process restart; see [SQLite evidence](SQLITE_INTERFACE.md).
The current suite adds 5 C03 domain tests, 13 C05 SQLite tests and 10 C14 SQLite
interface tests to the historical 38, for 66 total.

## Reproduce

After installing the package on Ubuntu 22.04:

```bash
python -m unittest discover -s tests/unit -v
python -m unittest discover -s tests/integration -v
python -W error::ResourceWarning -m unittest discover -s tests -v
python -m compileall -q src tests
python scripts/check_secrets.py
```

All commands above are offline and require no real API key. Restricted environments
may require permission to connect to loopback for Web tests. POSIX credential tests
skip on Windows; use Linux to exercise ownership, modes and symlinks.

## Remaining verification

D can independently complete browser interaction checks and additional malformed
HTTP request tests. They are not claimed as complete by the current CI result.

B owns SQLite lifecycle/migrations, memory and objective evaluation regressions.
A owns planning, delegation and real task E2E. C owns path/SSRF/Git scope, Docker
execution and platform guard tests. Their modules/suites remain integration
dependencies; no Docker/full-platform acceptance is claimed.

The secret-pattern check is a precaution, not proof that all possible secrets
are absent. Never commit real credentials.
