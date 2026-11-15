# Verification (C15)

## C10a local verification (2026-11-15)

C10a adds 32 tests: 14 harness/backend/broker tests, 9 real fake-CLI subprocess
protocol/lifecycle tests, 5 fixed-upstream proxy tests and 4 Docker command tests.
Full regression with `EVO_DOCKER_TESTS=1`: **232 tests, no skips**, on local
Ubuntu 24.04.4 x86_64 / Python 3.12.3 / Docker 29.1.3. Python 3.10 is also
verified locally. Default CI uses controllable CLI/proxy fixtures and no live key.

Explicit real Claude Code 2.1.311 smoke used the user's local third-party
development endpoint. It generated/read a Markdown file, generated a fixed Python
script, paused for a durable approval, executed that exact approved script in the
C09 container and restarted from recorded context. This is development evidence,
not formal DeepSeek/HKGAI backend or dataset evaluation evidence. Endpoint and
credentials are not included in the report. The local private report and trace
remain under ignored `.evo/harness-smoke/`.

The final smoke task was `smoke-dcdb19bdacaa47a28c879280eeaa05ac`, initial run
`b867e269600949dd8c38541c3731b3be`, resumed run
`3410c25db39a44e19e406141eab6e007`. All five objective smoke checks passed,
including exact Python stdout and one successful approved code execution.

Compileall, wheel build and packaged relay inclusion are checked. Ubuntu 22.04
remote CI and C11/C13 business integration remain pending. See
[C10a reproduction and boundaries](C10a_HARNESS.md).

## C10 local verification (2026-11-15)

Full regression with `EVO_DOCKER_TESTS=1`: **200 tests, no skips**, on local
Ubuntu 24.04.4 x86_64 / Docker 29.1.3 under both Python 3.12.3 and Python 3.10.21.
C10 adds 47 methods: 25 approval/executor tests, 13 Git scope tests, 6 real
Git/action container tests, 2 CLI/HTTP answer tests and 1 bounded stdin regression.
Tests cover concurrent one-shot decisions/claims, hashes/scope changes, denial,
expiry, safe audit results, malformed tool results, Git config/path/repository
rejection, explicit-file commits, disabled hooks and real temporary header URL
matching without GitHub access. All six Chromium browser groups passed, now
including a human approval click. See [C10 details and limits](C10_HITL_GIT.md).

Compileall for both Python versions, whitespace and secret-pattern checks pass.
No real remote push, token or LLM is used; push transport is controlled and the
real wrapper/config match is tested inside Docker. Remote Ubuntu 22.04 CI for
this commit remains pending. The Docker job pulls both pinned image digests and
saves `sandbox-environment` metadata. Without `EVO_DOCKER_TESTS=1`, nine real
Docker test methods are explicitly skipped.

## C09 local verification (2026-11-15)

C09 adds 10 unit tests in `unit/adapters/test_docker_command.py` and 3 opt-in
real Docker methods in `integration/test_docker_tools.py`. They cover bounded
non-root execution, mount/network/filesystem/capability isolation, timeouts,
output limits, exit status, fail-closed errors, environment isolation and cleanup.
Full regression with `EVO_DOCKER_TESTS=1`: **153 tests, no skips**, on local
Ubuntu 24.04.4 x86_64 / Docker 29.1.3, for both Python 3.12.3 and Python 3.10.21.
Compileall, diff whitespace and secret-pattern checks passed. Ordinary offline
runs skip the three real Docker methods; the dedicated Ubuntu 22.04 / Python 3.10
CI job requires them and saves environment/image metadata. Its remote execution
is still pending for this commit. See [C09 commands and limits](C09_DOCKER_SANDBOX.md).

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
artifact suites; C08 supplies SSRF/search suites; C09 supplies Docker unit and
real execution tests; C10 supplies approval/Git scope and container tests.
Full-platform integration remains a later milestone. Local Docker checks do not
establish full-platform acceptance.

The secret-pattern check is a precaution, not proof that all possible secrets
are absent. Never commit real credentials.
