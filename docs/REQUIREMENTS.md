# Requirements traceability

Updated: 2026-11-08 (Asia/Shanghai). D owns C01/C04/C14/C15/C16 in the team plan.
Current merged implementation baseline: main 4eb3097, including API commit 6cb3780.
The instructor's specification governs final acceptance; the C numbers are the
team's development breakdown, not five separate grading phases.

## D implementation and evidence

| Requirement / work item | Implementation | Evidence and remaining boundary |
|---|---|---|
| C01 install, single version, ignored runtime | pyproject.toml, evo.__version__, .gitignore | tests/test_baseline.py; installation/packaging passed in Ubuntu CI |
| C04 permitted DeepSeek/HKGAI providers | config.py, adapters/llm/ | test_llm.py and test_provider_diagnostics.py; HKGAI live text/JSON passed; DeepSeek live call unverified |
| HTTPS, bounded retries, object JSON | adapters/llm/client.py | Retry/auth/truncation/redirect/TLS tests; provider errors omit response bodies |
| Private credentials | adapters/llm/credentials.py, CLI hidden prompt | Credential tests incl. POSIX in Linux CI; no real credentials committed |
| Independent API configuration/checks | adapters/llm/factory.py, diagnostics.py | provider-info; check-provider --live; [live evidence](API_SMOKE_REPORT.md) |
| C14 run/DM/group/eval/metrics/trace/memory CLI | interfaces/cli.py | Routing fixtures pass; real A/B/C application gateway pending |
| Minimal task/inbox/DM/group/memory UI | interfaces/web/assets/index.html | HTTP tests pass; browser interaction acceptance pending |
| Host/Origin/CSRF/JSON writes | interfaces/web/app.py | Rejected write/no-side-effect tests pass; additional malformed-request coverage pending |
| Human review and linked revision | application/reviews.py | State/duplicate decision tests with in-memory repository; B's atomic persistence pending |
| C15 Linux offline CI and wheel checks | .github/workflows/ci.yml | [Actual successful PR #2 CI](https://github.com/sunry114/Evolutionary-Multi-Agent-System-ARIN5202/actions/runs/42449807107); 38 tests in source suite |
| C16 delivery docs and current evidence | SUBMISSION, TESTING, ARCHITECTURE, WORKFLOW | [Delivery overview](SUBMISSION.md); final integrated architecture/report/screenshots pending |

## Complete platform requirements

| Instructor requirement | Owner / expected implementation | Current acceptance state |
|---|---|---|
| Real human/Agent private and group chat | A chat + D interfaces | Interface exists; demo only stores human messages; Agent replies pending |
| Autonomous decomposition, role negotiation and delegation | A C03/C06/C11 | No real implementation in the documented baseline |
| Proactive clarification and critical-action approval | A/C + D inbox/answer entry | UI/dispatch exists; real pause/resume and persistence pending |
| Per-Agent memory and improvement from experience | B C05/C12 | Pending implementation and measured evidence |
| Per-Agent isolated filesystem/sandbox | C C07/C09 | Pending implementation and isolation tests |
| Search, file CRUD, code execution and Git tools | C C07-C10/C10a | Pending Agent tools; a developer's Git push does not satisfy this capability |
| Markdown/TXT only document tasks | C tool policy, team execution | No complex-format support added; real tool/output enforcement pending |
| Minimal functional UI | D C14 | CLI/basic Web exists; elaborate design is unnecessary |
| DeepSeek/HKGAI only; no OpenAI/Anthropic APIs | D C04 + A/C integration | Adapter selection restricted; course HKGAI verified; full system must preserve restriction |
| Automatic goal/subtask completion and execution time | B C13 + A task state | CLI query/assessment entry only; real metric computation pending |
| Official dataset, failure analysis, memory evolution report | B/team | [Evaluation report](EVALUATION_REPORT.md) is explicitly pending |
| GitHub code and sustained team contribution evidence | All members | D commits and merged PRs exist; full-semester/all-member screenshots still required |
| Comprehensive routing/memory/sandbox architecture | All members, D consolidates | Current component/contract docs exist; integrated design awaits implementations |
| Week 13 presentation | Whole team | Not delivered by these code/documentation PRs |

## Evidence navigation

- [Submission overview, commits and PR status](SUBMISSION.md)
- [Test inventory, limits and commands](TESTING.md)
- [API connection report](API_SMOKE_REPORT.md)
- [Integration contract](INTEGRATION.md)

CI success, human review, PR merge and final course acceptance are distinct facts.
No simulated result or human acceptance overrides objective evaluation.
