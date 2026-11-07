# Requirements traceability

Scope is the assignment in 职责分配.md: D owns C01/C04/C14/C15/C16.
Paths below are relative to the repository. Pending rows are explicit dependencies.

| Requirement | Implementation / owner | Evidence / current limitation |
|---|---|---|
| C01 install, version, ignored state | pyproject.toml, evo.__version__, .gitignore | tests/test_baseline.py; Linux CI configured |
| C04 DeepSeek/HKGAI only | src/evo/config.py, adapters/llm/ | tests/unit/test_llm.py |
| HTTPS, timeout, bounded retries, object JSON | adapters/llm/client.py | transient/auth/exhaustion/truncation tests |
| Private credentials | adapters/llm/credentials.py | tests/unit/test_credentials.py; POSIX tests run in Linux only |
| C14 run/DM/group/eval/metrics/trace/memory CLI | interfaces/cli.py | tests/integration/test_interfaces.py routing; backend pending |
| Task/inbox/DM/group/memory Web views | interfaces/web/assets/index.html | Web HTTP integration; manual browser QA pending |
| Host/Origin/CSRF/JSON writes | interfaces/web/app.py | rejected-write/no-side-effect tests |
| Review/revision status separation | application/reviews.py | review/revision/conflicting decision tests |
| Durable task/trace/review | B repository, see INTEGRATION.md | pending actual store; no false persistence claim |
| Durable human approvals | A/C dispatch answer | pending backend integration |
| C15 Linux CI / wheel / offline tests | .github/workflows/ci.yml | workflow execution depends on GitHub push |
| SQLite/planning/path/SSRF/Git/Docker regressions | A/B/C suites | pending modules and Docker CI job |
| C16 architecture/workflow/install docs | README, ARCHITECTURE, WORKFLOW | current implemented scope described |
| Official evaluation / memory ablation | B evaluation | EVALUATION_REPORT.md explicitly pending |
| Real contribution history | commits authored Xiaowen612 | SUBMISSION.md; remote push subject to repository permission |
| Agent planning / roles / delegation | A (C03/C06/C11) | not present in starting remote |
| Memory / data / evaluation | B (C02/C05/C12/C13) | not present in starting remote |
| Workspace / Web tools / sandbox / Git / Harness | C (C07-C10/C10a) | not present in starting remote |

D's independently testable implementation is delivered. Full C14/C15/C16 acceptance
requires the listed dependencies, Linux execution, team review and official data.
