# Member D integration contract

This branch was built against an empty code repository. The following interfaces
are explicit proposals for A/B/C to review, not claims that their work is present.

The default is now interfaces.sqlite_application.SQLiteApplication, supporting
real SQLite reads/review/revision, ChatService DM/group and durable human answers. Set
EVO_APPLICATION_FACTORY=your_package.bootstrap:create_application to override it.
The factory accepts state=Path and returns ApplicationGateway. It must be
thread-safe: Web uses concurrent request handlers, including human answers while
a run waits. Import/application construction failures fail clearly.

snapshot() returns a JSON object:
- mode: display label (production or demo).
- agents: list of agent ID strings or objects with id.
- tasks: list of {id,prompt,status,result,review_status,revision_task_id?}.
- messages: list of {kind:dm|group,agent,sender,content}.
- memory: authorized human-readable records, never cross-agent executor access.
- requests: pending {id,task_id,agent,kind:approval|clarification,payload}.

dispatch(operation, payload) returns a JSON object; ValueError/KeyError indicate
invalid input or state conflict. Validate schema and permissions in the backend,
not just UI. No operation may return credentials, provider configuration secrets,
or raw exception bodies. CLI exits 1 for failed/blocked/unavailable or passed=false,
2 for request/backend errors, 0 otherwise. A complete evaluation adapter must
return passed=false if any required objective case fails.

| Operation | Payload | Owner |
|---|---|---|
| run | prompt | A orchestrator |
| dm | agent,message | A chat |
| group | message | A chat |
| eval | dataset,output,no_memory | B evaluation |
| trace, metrics | task_id | B query |
| memory | agent | B query |
| review | task_id,decision,feedback | D ReviewService + B atomic store |
| revision | task_id,prompt | D ReviewService + B atomic new-task/link + A scheduling |
| answer | request_id, approved:boolean OR answer:string | C approvals + A resume |

ReviewRepository in application/reviews.py is the minimal B contract. Persist
tasks, reviews, decisions and revision links transactionally. Recheck expected
state atomically; create the new task and link in one transaction. On retry after
a lost HTTP response, return conflict or an idempotent existing result, never
create a duplicate child. Preserve technical status. Then A schedules linked
created tasks. The UI accepts reviewing only successful tasks with pending review.

The --demo implementation is explicitly in-memory and always blocks execution.
It does not call LLMs or implement an agent. Closing the process loses all demo
state. SQLite task/trace/review persistence and actual server-process restart are now
covered by test_sqlite_interfaces.py. C10 now supplies pending approval answering;
automatic model-loop recovery still requires C11 integration. Do not present preview records
as evaluation evidence.


## LLM integration after the API follow-up
A can import evo.adapters.llm.factory.create_provider instead of separately
constructing HTTP clients. It reads the documented non-secret environment config.
The verified HKGAI completion endpoint is /v1/chat/completions. Catch ProviderError
and persist only its safe code, not third-party raw response bodies. CredentialError
indicates missing/unsafe credentials. Credentials remain outside ApplicationGateway
snapshots, task payloads, trace and reports. check-provider is connectivity evidence
only; A still owns the agent loop and B the objective evaluation.

## C10a harness adapter

Use `create_harness(HarnessConfig(adapter='claude_code'), state, broker,
model=model, credential_loader=loader)` with a trusted `PlatformToolBroker` bound
to the task owner. State must share the SQLite state root. `RunRequest` carries
task/agent/session/workspace/role/authorized memory. Poll `wait` and `events`;
answer durable approval requests through the existing CLI/Web, then explicitly
call `resume(result.resume_ref, original_request)`. Recovery restarts the CLI from
recorded platform outcomes, with a fresh native session ID. C11 must verify the
goal independently; a successful harness never marks the task successful.
See [API example and development endpoint setup](C10a_HARNESS.md).

## C05 storage adapter

`evo.adapters.persistence.sqlite_store.SQLiteStore` implements the durable
repository operations needed by task, message, trace, approval and review
integration.

Important error behavior:

- `RecordNotFoundError` is also a `KeyError`;
- `ConflictError` is also a `ValueError`;
- duplicate IDs, stale expected review states and duplicate revisions are
  conflicts;
- foreign-key failures are reported as safe conflicts without exposing raw
  database details.

`get_task(task_id)` returns a JSON-compatible mapping including technical
`status`, optional `review_status`, review feedback and the linked revision task
ID. A successful task can therefore be passed directly to `ReviewService`.

Storage methods do not determine which actions require approval. They persist
requests, decisions and audit metadata and atomically claim actions; C10's
application service owns approval policy and answer processing.
C12 owns memory recall and learning behavior.

Current default workspace behavior and read-query extensions: [SQLITE_INTERFACE.md](SQLITE_INTERFACE.md).

## C07 file adapters

Construct `WorkspaceFilesystem(state, owner)` and `ArtifactExchange(workspace)`
in trusted application code. `WorkspacePort` and `ArtifactPort` in `ports/tools.py`
describe their methods; `Artifact` carries ID, owner, original name, purpose,
byte size, SHA-256 and recipients. Select the instance from trusted task ownership,
never a model-supplied source agent. Only the workspace directory may be mounted
into C09 containers. Code/config/data require explicit purpose; user reports
must use the default `document` purpose and `.md`/`.txt`.

Publish needs explicit recipients; import checks authorization and integrity and
never overwrites an existing destination. C10/C11 must derive `delete_file`'s
`approved=True` from a validated human decision, never forward it from model
arguments. The adapters do not implement the task dispatcher, approval waiting,
or Docker execution. See [usage, limits and test evidence](C07_WORKSPACES.md).

## C08 web adapters

`WebTools(task_id)` implements `WebPort.search(query)` and `fetch_url(url)`.
Keep one instance per trusted task, shared across agents. Failed searches consume
the three-call budget; never reconstruct per action or accept limits/resolver/parser
from model arguments. C11 owns durable budget recovery and action audit records.
`SearchResult` and `WebPage` are frozen records with `untrusted=True`; preserve
their URL/source metadata and treat text as tool data, never system instructions.
`WebToolError` exposes safe messages without raw network errors or response bodies.
This adapter neither writes files nor schedules tasks. See [C08](C08_WEB_TOOLS.md).

## C09 Docker Python adapter

`DockerPythonRunner(workspace, image=DEFAULT_IMAGE, timeout=30,
max_output_bytes=65536)` implements `PythonRunnerPort.run_python(path,
approved=False)`. Select `workspace` from trusted task ownership, and supply
`approved=True` only after C10 validates a durable decision for that exact action.
Neither agent selection nor approval flags may be forwarded from model arguments.

`ExecutionResult` contains `exit_code`, `stdout`, `stderr`, `timed_out` and
`output_limited`. C11 must treat timeout/output-limit/nonzero exit as unsuccessful
and preserve safe audit metadata. Output is untrusted and may contain sensitive
workspace content; redact before logging. Infrastructure failures expose safe
`ToolError`; denied execution exposes `PermissionError`; file policy errors use
`ValueError`. No automatic image pull or host execution fallback occurs.

The runner uses the local system Docker socket, digest-pinned Python image and
only the agent workspace mount. Its temporary Docker config excludes host proxy
and credential configuration. Git/network policy is C10 work; the C09 command
builder is intentionally restricted to network-disabled Python execution.
See [C09 setup, resource limits and recovery caveats](C09_DOCKER_SANDBOX.md).

## C10 approvals and single-action execution

`ApprovalService(store)` implements request creation, explicit human answers,
scope validation, expiry and one-use claims. SQLiteApplication's `answer`
operation is now enabled: exactly `request_id` plus `approved:boolean` or
`answer:string`; the existing CSRF/Host/Origin protections apply. A successful
answer persists a decision only and never executes a tool automatically.

`ActionExecutor(store, approvals, agent=..., workspace=..., python=..., git=...,
require_commit_approval=False)` is the C11 single-action integration point.
Only trusted assigned owners with running tasks can start actions. Critical
operations return waiting_human/request_id until a decision exists. Resume with
the identical call, optionally specifying the request_id; scope/content changes,
replays and expired decisions cannot authorize execution. Outcomes are JSON-safe;
success concerns the action, while the goal remains running for C11 verification.
Persisted results contain only safe exit/timeout/output-limit/error metadata.

`DockerGitTools` implements `GitPort`. Explicit commit paths are required; the
adapter rejects custom/executable repository config and uses no network for local
operations. To enable approved push, trusted bootstrap code must configure a
`RepositoryToken(exact_repository, loader)`; model arguments never include tokens.
Push uses fixed commit/refspec and URL-scoped temporary authorization over stdin.
See [C10 API, supported arguments and lifecycle](C10_HITL_GIT.md).


## C14 live gateway (2026-11-15)
Default SQLiteApplication now composes ChatService, ReviewService and ApprovalService.
It exposes dm, group, messages and answer in addition to stored task operations.
Create it with provider= for dependency injection in offline tests; normal calls
lazily use create_provider() and external credentials. Agent registry defaults are
DEFAULT_AGENTS; pass agents= for an explicit alternative. Chat failure returns a
safe ApplicationFailure (HTTP 502/CLI 1), credential absence returns unavailable
(HTTP 503/CLI 2), bad input/state returns 409/CLI 2. Human decisions are saved;
execution resume remains C11's responsibility.

snapshot.capabilities drives disabled controls. Current run/memory/eval/metrics
remain false. A/B can supply a full factory with those methods; do not mark them
available until they have real implementations. messages optionally accepts agent
and returns bounded persisted human-visible history. DM data must not enter group
or another Agent's prompt. See [C14_STATUS.md](C14_STATUS.md) for acceptance evidence.
