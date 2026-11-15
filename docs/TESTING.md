# Verification (C15)

## C08 local verification (2026-11-15)

C08 adds 29 offline tests: 10 search/parser methods in
`unit/adapters/test_search_parser.py` and 19 methods in `security/test_ssrf.py`.
They exercise public retrieval, unsafe targets/DNS/redirects, pinned IP with original
TLS hostname, MIME/encoding/framing/body bounds, DNS saturation, real HTTPResponse
parsing and wall deadlines using local Unix socketpair without TCP/network access.
See [C08 contracts and limits](C08_WEB_TOOLS.md). Full regression: **140 tests,
no skips**, with ResourceWarning promoted to an error on Ubuntu 24.04.4 /
Python 3.12.3. All 29 C08 tests also passed on local Python 3.10. Compileall,
diff whitespace and secret-pattern checks (including new untracked source) passed.
Ubuntu 22.04 / Python 3.10 CI and live-provider availability remain separate evidence.

## C07 local verification (2026-11-15)

C07 adds 23 Linux tests: 11 in `security/test_path_boundary.py` and 12 in
`integration/test_artifact_exchange.py`. They cover workspace CRUD and policy,
path/symlink/hardlink/special-file rejection, UTF-8 byte limits, descriptor cleanup,
recipient permissions, snapshot copies, restart, integrity, concurrent imports,
publication failure cleanup and C05 coexistence. See [C07](C07_WORKSPACES.md).
The local environment is Ubuntu 24.04.4 x86_64 / Python 3.12.3; this is developer
verification, not a new Ubuntu 22.04 CI or official evaluation claim.
Full regression passed: **111 tests, no skips**, with ResourceWarning promoted
to an error. Compileall, diff whitespace checks and the tracked secret-pattern
check also passed.

Previous browser milestone (2026-11-14): 74 Python methods, 72 passed and 2 POSIX skips
on Windows. Eight new raw-TCP methods live in tests/security/test_http_boundaries.py.
Six separate real-browser acceptance groups passed in Edge; Chromium runs in its
own Ubuntu CI job. See [WEB_ACCEPTANCE.md](WEB_ACCEPTANCE.md) for evidence and commands.
The dated tables below are historical milestones.

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

Browser interaction and the documented malformed-request checks are now covered
for the SQLite workspace; see WEB_ACCEPTANCE.md. New full-backend flows still
require additional acceptance when integrated.

B owns SQLite lifecycle/migrations, memory and objective evaluation regressions.
A owns planning, delegation and real task E2E. C07 now supplies the file path and
artifact suites; C08 supplies SSRF/search suites. Git scope and Docker execution
tests remain later milestones.
No Docker/full-platform acceptance is claimed.

The secret-pattern check is a precaution, not proof that all possible secrets
are absent. Never commit real credentials.
