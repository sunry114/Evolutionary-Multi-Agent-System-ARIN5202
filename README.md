# Evolutionary Multi-Agent System

HKUST ARIN5202 group project. Supported deployment and assessment environment:
**Ubuntu 22.04 LTS, x86_64, Python 3.10**. Agent execution requires Linux Docker
Engine; native Windows/macOS execution is not a supported assessment environment.

## Development status

The original remote contained only README and LICENSE, despite describing a
working prototype. Development began with member D's assigned work (C01, C04,
C14, C15, C16), and now includes C03 domain contracts, C05 SQLite persistence,
C06 collaboration services, C07 isolated workspaces/artifact exchange, C08
bounded search/text retrieval, C09 fail-closed Linux Docker execution and C10
durable approval/single-action execution with container Git tools.
C10a now adds interchangeable Baseline/Claude Code harnesses, platform MCP tools,
durable approval pause/recovery and an isolated native CLI runtime. See
[C10a API, boundaries and explicit live smoke](docs/C10a_HARNESS.md).
C11 orchestration, C12 private memory and the C13 evaluation library are now
implemented. The default application has a C11 execution factory for `run` and
explicit `resume`, and a C13 `eval` gateway. Formal results remain pending.

## Install and verify

```bash
git clone https://github.com/sunry114/Evolutionary-Multi-Agent-System-ARIN5202.git
cd Evolutionary-Multi-Agent-System-ARIN5202
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m evo --help
evo-v3 --version
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

No API keys are required for unit tests. Keep runtime state under ignored `.evo/`.
Use real commit authors and dates; no reconstructed or backdated contribution
history. Team members must review integration changes before merging to main.

## Preview D interfaces

```bash
python -m evo --demo web
```

Open http://127.0.0.1:8768. Preview has tasks, inbox, group chat, private chat and
memory screens. It is **in-memory demo mode**: submitting a task records a blocked
preview and never pretends an agent executed it. Refresh updates state. Close the
server with Ctrl+C. For the CLI preview:

```bash
python -m evo --demo run 'Create hello.md'
# Exits 1 with status=blocked because this is not real execution.
```

Windows users can run the execution environment in WSL without replacing Windows.
See [local Linux installation and launch](docs/LOCAL_LINUX_SETUP.md) for the installed
environment, HKGAI Harness protocol bridge, and real approval/execution smoke check.

## Configure HKGAI on Linux

The instructor-provided service is https://api1.hkgai.net. The verified API base
is **https://api1.hkgai.net/v1**, with model t2_hkgai-v3_fp8_1m_e7. Text and JSON
connection checks passed; see [API smoke evidence](docs/API_SMOKE_REPORT.md).
Enter your key interactively without putting it into source or shell history:

```bash
python -m evo setup-key --provider hkgai
```

This creates ~/.config/evo-collab/hkgai.key with private POSIX permissions and
refuses overwriting an existing key. Alternatively set HKGAI_API_KEY through your
local environment configuration. No real key is included in this repository.
See [provider configuration](docs/PROVIDERS.md).

## Run a verified task

The default application now wires C11 to the permitted provider and a scoped
Harness. A simple one-subtask CLI run needs a human-supplied objective check:

```bash
EVO_PROVIDER=deepseek python -m evo run 'Create hello.md containing hello' \
  --expect-file hello.md --expect-text hello
python -m evo answer REQUEST_ID --approve
python -m evo resume TASK_ID
```

Install the supported native Claude CLI and configure its path with
`EVO_CLAUDE_CLI` if needed. The configured DeepSeek/HKGAI key must be available
through the documented credential loader. HKGAI Anthropic-wire compatibility
has not yet been verified. Critical actions still require explicit approval;
answering does not execute them until `resume` is called. The Web `run` JSON
payload accepts `checks` keyed by planned subtask ID for multi-step tasks.

Configure scenario roles before a mid-test run. The role list is saved in
`.evo/agents.json`; each task records its own role snapshot for `resume`.

```json
[
  {"name": "researcher", "specialty": "Find and summarize evidence"},
  {"name": "architect", "specialty": "Design the solution"},
  {"name": "builder", "specialty": "Create deliverable files"},
  {"name": "reviewer", "specialty": "Check the deliverables"}
]
```

```bash
python -m evo --state .evo agents --file scenario_agents.json
python -m evo --state .evo group 'Create report.md containing hello' \
  --execute --expect-file report.md --expect-text hello
# For several subtasks, checks.json maps s1..s5 to artifact checks.
python -m evo --state .evo run 'Create the deliverables' --checks-file checks.json
```

`group --execute` saves all role replies, then runs the original human goal
with objective checks. Plain `group` remains a discussion. Formal mid-test
results still require the released dataset, native Harness runtime, provider
credentials and an official dataset.

## Connect a custom backend

A/B/C provide a factory accepting state=Path and returning ApplicationGateway:

```bash
export EVO_APPLICATION_FACTORY=your_package.bootstrap:create_application
python -m evo web
python -m evo run 'Create hello.md and verify contents' --expect-file hello.md --expect-text hello
python -m evo dm builder 'Summarize your role'
python -m evo group 'Discuss how to divide this task'
python -m evo trace TASK_ID
python -m evo metrics TASK_ID
python -m evo memory builder
python -m evo review TASK_ID accepted
python -m evo review TASK_ID changes_requested --feedback 'Fix the output'
python -m evo revision TASK_ID 'Apply the feedback'
python -m evo answer REQUEST_ID --deny
python -m evo eval DATASET.jsonl --output .evo/reports/report.json --no-memory
```

Without an override, the default SQLite application supplies the real C11
execution factory. `eval` writes reports under `.evo/reports/`; `metrics` remains
unavailable. See [SQLite integration](docs/SQLITE_INTERFACE.md). The Web
server only binds loopback. Use the printed 127.0.0.1 URL and keep the terminal
open. Never expose it publicly without a separate reviewed authentication layer.

## Team handoff

Start with **[D 部分交付总览与证据](docs/SUBMISSION.md)** for the latest
implementation status, merged PRs, CI links and remaining work. Updated 2026-11-08:
PR #1 and #2 are merged; the API milestone contained 38 tests (current SQLite milestone: 66 tests); real HKGAI text and
JSON checks passed. See the linked report for environments and verification limits.

- [Integration contract](docs/INTEGRATION.md) — operations and A/B/C dependencies.
- [Architecture](docs/ARCHITECTURE.md), [workflow](docs/WORKFLOW.md).
- [Requirements matrix](docs/REQUIREMENTS.md), [testing](docs/TESTING.md).
- [Evaluation status](docs/EVALUATION_REPORT.md), [submission evidence](docs/SUBMISSION.md).

D interfaces are independently testable. Full platform acceptance, execution/approval
resume, official datasets and memory ablation await
team integration. Actual successful Ubuntu CI runs are linked in the delivery
overview; their success does not establish full-platform acceptance.


## Independent API diagnostics (no A/B/C backend required)

```bash
python -m evo provider-info
python -m evo check-provider --live --ask-key
python -m evo check-provider --live --json-object --ask-key
```

provider-info validates configuration offline and never reads a key. A live check
requires --live, makes one fixed request with a 64-token output limit, and may
incur provider usage. --ask-key reads a temporary key through a hidden prompt and
does not save it. Omit --ask-key to use the provider environment variable or the
private Linux credential file. Output is a non-secret summary, error category and
elapsed time; it never includes the model response body. Exit 0 means expected
reply, 1 means a failed smoke check, 2 means a CLI/configuration error.

Optional non-secret environment settings: EVO_PROVIDER (hkgai or deepseek),
EVO_MODEL, EVO_BASE_URL, EVO_TIMEOUT (seconds), EVO_RETRIES. Custom endpoints remain
rejected; changing environment variables cannot enable development mode.

Agent backend integration can use the same configuration:

```python
from evo.adapters.llm.factory import create_provider
provider = create_provider()
result = provider.complete(
    [{"role": "user", "content": "Return a JSON object describing your plan."}],
    json_object=True,
)
```

This adds real API connectivity, not the missing orchestration/memory/tools.


## Real SQLite workspace (C14)

```bash
python -m evo --state .evo web
python -m evo --state .evo tasks
python -m evo --state .evo task TASK_ID
python -m evo --state .evo trace TASK_ID
```

The default workspace opens .evo/events.sqlite3 and shows real stored task/review
records. CLI review/revision now use atomic SQLite operations. Data survives
process restart; a revision remains created until A's scheduler is connected.
A new database is empty. The UI disables unavailable task/chat execution; C10
enables explicit human approval/clarification answers without automatic execution.
Set EVO_APPLICATION_FACTORY to override this with the team's complete backend;
--demo still selects the explicitly volatile preview.

See [SQLite usage, tests and limitations](docs/SQLITE_INTERFACE.md).


## Browser and HTTP acceptance

The SQLite workspace now has real-browser acceptance and raw-TCP boundary checks.
See [WEB_ACCEPTANCE.md](docs/WEB_ACCEPTANCE.md) for reproduction and scope.
The browser milestone had 74 Python test methods; six browser groups run separately.
The Ubuntu workflow has a browser job using pinned Playwright and Chromium,
with screenshots/report artifacts. These are isolated fixtures, not Agent results.

## C07 workspace and artifact tools

`WorkspaceFilesystem` binds file operations to one agent's private
`.evo/agents/<agent>/workspace/`. It enforces relative paths, no symlinks/hardlinks,
regular UTF-8 files, a 1 MiB limit and explicit document/code/config/data purposes.
Reports use `.md`/`.txt`. `ArtifactExchange` publishes recipient-scoped snapshots;
authorized agents import independent copies with SHA-256 verification and no
overwrite. These adapters are ready for the C11 tool broker; default CLI/Web
execution is unchanged. See [C07 usage and safety tests](docs/C07_WORKSPACES.md).

## C08 search and text retrieval

`WebTools(task_id)` provides task-scoped DuckDuckGo search and public HTTPS text
retrieval, with bounded requests, redirect/DNS checks, pinned IP connections and
verified TLS. Search results retain URLs; fetched text is labeled untrusted.
Offline tests cover positive retrieval, SSRF rejection, resource limits and
concurrent search budgets. C11 owns dispatcher integration and persistent budget
recovery. See [C08 contracts, limits and tests](docs/C08_WEB_TOOLS.md).

## C09 Linux Docker execution

`DockerPythonRunner(workspace)` runs approved Python files inside a non-root,
network-disabled container with a read-only root filesystem, one bound agent
workspace and CPU/memory/PID/output/time limits. Docker errors fail closed;
there is no host execution fallback. The default Python image is pinned by digest.
Prepare it and explicitly enable the real Docker tests:

```bash
image="$(python -c 'from evo.adapters.tools.linux import DEFAULT_IMAGE; print(DEFAULT_IMAGE)')"
docker --host unix:///var/run/docker.sock pull "$image"
EVO_DOCKER_TESTS=1 python -W error::ResourceWarning -m unittest \
  tests.unit.adapters.test_docker_command tests.integration.test_docker_tools -v
```

The runner uses the local system Docker socket and requires a non-root host
UID/GID with access to that socket. C10 owns durable approval decisions; C11 owns
dispatch and task audit. Default CLI/Web execution remains unavailable.
See [C09 usage, limits and test evidence](docs/C09_DOCKER_SANDBOX.md).

## C10 approvals and container Git

`ApprovalService` persists decisions and `ActionExecutor` gates `run_python`,
`delete_file` and `git_push`; high-risk policy can also gate `git_commit`.
Requests bind task/agent/parameters and file hashes or commit SHA, expire after
one hour by default and can be consumed only once. Denial blocks the task.
The default SQLite CLI/Web can now answer approval and clarification requests:

```bash
python -m evo --state .evo answer REQUEST_ID --approve
python -m evo --state .evo answer REQUEST_ID --deny
python -m evo --state .evo answer CLARIFICATION_ID --text 'report.md'
```

`DockerGitTools` provides isolated init/status/explicit-file commit and approved
GitHub HTTPS push. It rejects unsafe repository config, disables hooks/helpers,
and injects a repository-scoped token through stdin into temporary Git process
configuration. Local Git operations use no network; only push uses bridge
network with validated targets and redirects disabled. Prepare its pinned image:

```bash
image="$(python -c 'from evo.adapters.tools.git import DEFAULT_GIT_IMAGE; print(DEFAULT_GIT_IMAGE)')"
docker --host unix:///var/run/docker.sock pull "$image"
```

Human answers persist without invoking tools automatically; C11 owns scheduling
and the complete Agent loop. See [C10 contracts, security boundaries and tests](docs/C10_HITL_GIT.md).


## Current C14: live chat and human answers

[C14 status and evidence](docs/C14_STATUS.md) is the current acceptance checklist.
The default workspace now supports real DM/group replies through the course HKGAI
provider, saved histories and ApprovalService answers. Configure credentials using
setup-key or the provider environment variable. No key is needed for stored task
queries, review/revision or answering existing requests.

```bash
python -m evo --state .evo dm builder 'Describe your role.'
python -m evo --state .evo group 'Discuss how to divide a review task.'
python -m evo --state .evo messages --agent builder
```

Automatic goal execution and explicit resume (C11) now use the execution factory.
C12 memory is connected to task execution. C13 `eval` is callable through the
CLI/Web API; `metrics` remains unavailable. Chat replies are not counted as
task completion.


## Mid Test and current D handoff (2026-11-18)

Start with [MID_TEST_RUNBOOK.md](docs/MID_TEST_RUNBOOK.md). The instructor ZIP uses
sequential role-based scenarios, not independent JSONL cases. Prepare it privately
with prepare-midtest; audit exported evidence with audit-midtest. Both commands
require a new output under ignored .evo and never execute or auto-score tasks.

C11/C12/C13 runtime wiring is now in main. CLI memory supports --query, metrics
shows stored task timing/completion snapshots, and Web has memory/metrics/JSONL
evaluation controls. Formal scenario evaluation remains B's C13 responsibility;
D provides entries and delivery evidence. Chat-only exports cannot establish
actual execution time or task completion. See the runbook for the Linux/Docker
prerequisites and the HTML-versus-Markdown/TXT constraint conflict.
