# Requirements traceability

Latest C14 milestone (2026-11-15): ChatService-backed DM/group chat, persisted
history and ApprovalService answers are now connected. See [C14_STATUS.md](C14_STATUS.md)
for current validation, limitations and remaining C11/C12/C13 dependencies.
Earlier dated counts and unavailable-chat statements below are historical.

Updated: 2026-11-08 (Asia/Shanghai). D owns C01/C04/C14/C15/C16 in the team plan.
Current merged implementation baseline: main 4eb3097, including API commit 6cb3780.
The instructor's specification governs final acceptance; the C numbers are the
team's development breakdown, not five separate grading phases.

## C10a pluggable Agent Harness (2026-11-15)

- Harness port, explicit Baseline action adapter, Claude Code 2.1.311 adapter and registry implemented.
- Platform MCP bridge preserves C07-C10 tool, ownership and approval boundaries.
- Networkless CLI container, fixed-upstream model proxy, scoped context recovery,
  task execution lease and cleanup quarantine implemented and tested.
- Explicit local third-party development smoke passed; formal backend validation,
  C11/C13 integration and Ubuntu 22.04 remote CI remain separate evidence.

See [C10a contracts and verification](C10a_HARNESS.md).

## C10 durable approvals and secure Git operations (2026-11-15)

`application/approvals.py` and `executor.py` implement exact-action, one-use human
approval with persistent decisions, concurrent claims, expiry and denial blocking.
Default CLI/Web can now answer requests; answering alone never invokes a tool.
`adapters/tools/git.py` runs init/status/explicit-file commit in network-disabled
containers and gates a scoped GitHub HTTPS push with temporary stdin credentials.
Local regression and real Git/approved Python execution pass; real remote push,
remote baseline CI and the C11 complete task loop remain unverified/pending.
See [C10 contracts and evidence](C10_HITL_GIT.md).

## C09 fail-closed Linux Docker execution (2026-11-15)

`adapters/tools/docker.py` and `linux.py` supply approved Python execution in a
non-root, network-disabled, resource-bounded container mounting only the bound
agent workspace. The default image is digest-pinned; host Docker configuration
and credentials are excluded. Ten unit tests and three opt-in real Docker tests
cover isolation, failure paths, output/time limits and cleanup.
See [C09 contracts and evidence](C09_DOCKER_SANDBOX.md). C10 supplies the approval
lifecycle; C11 full dispatch and remote Ubuntu 22.04 CI acceptance remain separate work.

## C08 bounded search and retrieval (2026-11-15)

`adapters/tools/web.py` supplies task-scoped search and text retrieval with public
HTTPS/443 enforcement, all-address DNS validation, pinned IP/TLS connections,
redirect checks, bounded responses/time/searches and explicit untrusted source
metadata. 29 offline tests cover parser behavior, SSRF and transport boundaries.
C11 dispatcher and persistent search budget recovery remain pending.
See [C08](C08_WEB_TOOLS.md); no live-search or official evaluation claim is made.

## C07 workspace isolation and artifact exchange (2026-11-15)

`adapters/tools/filesystem.py` and `artifacts.py` implement private bound
workspaces, bounded UTF-8 files, purpose-specific suffixes, path/link rejection,
and explicit recipient-scoped publish/import with snapshot digest verification.
23 new tests cover path boundaries, independent copies, restart, corruption,
concurrent no-clobber imports and C05 coexistence. See [C07](C07_WORKSPACES.md).
Docker/process isolation is supplied by C09 and approvals by C10; full task-loop
integration remains C11 work.

## C06 planning and collaboration (2026-11-14, PR #9 branch)

The C06 service layer provides private DM history, group chat separated from task
logs, a validated 1–5 subtask DAG, and all-agent bids with a stable winner. Bid
events include `subtask_id`, so bids for sibling subtasks on one task are
attributable. `tests/unit/application/` covers these rules.

The planner treats a named required input as missing when its value is `None`,
blank text, or an empty collection. It creates a clarification request before
calling the model. For goals with no declared required inputs, the model must
ask one question when a specific artifact, target, or constraint needed for a
verifiable result is absent or ambiguous. A question requires a request store
and `task_id`; the planner raises an error instead of returning an untracked
question. `StoredClarificationRequests` writes a pending request through the
existing approval repository. `tests/integration/test_c06_clarification.py`
reopens SQLite and checks the request, payload, pending inbox, and audit event.

C06 is a service-level milestone. Default CLI/Web dispatch and the task loop do
not yet call these services; C11 owns that integration and answer/resume behavior.
These tests do not establish full platform or official dataset acceptance.

## Browser and HTTP acceptance follow-up (2026-11-14)

Current SQLite browser review/navigation/revision/trace/mobile checks and HTTP
boundary tests are complete within the scope in [WEB_ACCEPTANCE.md](WEB_ACCEPTANCE.md).
A real browser found a disappearing-trace defect that is fixed and covered by
acceptance. The previous pending browser/boundary entries below are historical;
they do not describe the current SQLite milestone. The suite now has 74 Python
methods plus six separate browser acceptance groups. Full platform integration
and official evaluation remain pending.

## Current C14 SQLite follow-up (2026-11-13)

C03/C05 are now merged in main cbac88e. Default CLI/Web uses real SQLite for stored
task/trace queries, review and linked revision. Process restart is tested with an
actual Web child process. Agent execution and approval resume remain unavailable.
See [SQLITE_INTERFACE.md](SQLITE_INTERFACE.md); local suite is now 66 tests.
The baseline tables below describe the earlier API milestone, not current absence
of C03/C05. B owns the persistence implementation; D adds the interface bridge.

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
| Autonomous decomposition, role negotiation and delegation | A C03/C06/C11 | C06 planner and all-agent bidding services exist on PR #9; C11 task-loop scheduling and CLI/Web integration pending |
| Proactive clarification and critical-action approval | A/C + D inbox/answer entry | C06 planner questions and C10 durable human answers/single-action gates tested; C11 automatic model-loop resume pending |
| Per-Agent memory and improvement from experience | B C05/C12 | Pending implementation and measured evidence |
| Per-Agent isolated filesystem/sandbox | C C07/C09 | C07 workspace/artifact boundaries and C09 Docker isolation tested locally; remote baseline CI and task-loop integration pending |
| Search, file CRUD, code execution and Git tools | C C07-C10/C10a | C07 files/artifacts, C08 web, C09 Python and C10 approval/Git adapters tested; real remote push unverified, task-loop integration pending |
| Markdown/TXT only document tasks | C tool policy, team execution | C07 enforces document suffixes separately from code/config/data; full task-output integration pending C11 |
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
