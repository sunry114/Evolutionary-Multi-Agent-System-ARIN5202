# C11 orchestration handoff

The `Orchestrator` in `src/evo/application/orchestrator.py` connects C06 planning
and bidding to the C10a Harness. It uses the C05 SQLite store for task state,
trace events, clarification requests and agent-to-agent messages. The caller
injects a Harness factory and a workspace factory bound to the same state root.

## Execution contract

- `run(goal, checks=..., actions=...)` creates a root task, persists its plan and
  executes ready subtasks serially. `actions` is only for the deterministic
  Baseline Harness; a model-driven Harness can leave it empty.
- Every successful file subtask needs a caller-supplied `ArtifactCheck` with an
  expected exact string or substring. The coordinator reads the bound agent
  workspace and checks the SHA-256 against an artifact reported by the Harness.
  Harness success or a model-authored evidence string alone cannot complete it.
- Failed or blocked prerequisites block their successors. Provider, assignment,
  execution, step and search limit failures are recorded as technical failures.
- A pending planner question or critical tool approval leaves the root at
  `waiting_human`. Human decisions are saved through C10. `resume(root_id)`
  replays the stored plan or uses the C10a Harness checkpoint; it does not grant
  approval itself. Pending approval cannot be resumed early.
- The optional `delegate` platform tool is exposed only when the trusted broker
  receives a delegation callback. The coordinator rejects self or unknown Agent
  targets, depth above 2 and total task count above 12. Request and response
  messages are saved with the delegated task.
  A delegated child that needs human approval stops its parent rather than
  silently succeeding; nested approval resume needs a separate recovery flow.

## Validation and current boundary

`tests/e2e/test_collective_task.py` covers plan → bids → execution → evidence →
trace, dependency failure, clarification, delegation, limits and missing evidence.
`tests/e2e/test_c11_harness_resume.py` uses the real Baseline Harness, C10 approval
and SQLite to pause and resume an approved Python action.

This is the C11 application layer. `interfaces/execution_factory.py` now wires
the default CLI/Web `run` and explicit `resume` operations to the configured
provider, per-Agent workspace, tool broker, approval service and Harness.
The caller must provide trusted per-subtask artifact checks; CLI flags support
one simple `s1` file check or a JSON mapping through `--checks-file`, while the
Web API accepts a check mapping. `agents --file` and the Web role form persist
1–8 scenario roles in `.evo/agents.json`. Each task saves its own role snapshot
for resume, even if workspace configuration later changes. `group --execute`
and the Web group execution option collect the discussion, then start `run`
with the human goal and objective checks.
Native Claude CLI and provider credentials are required for a live run.
HKGAI's Anthropic-wire compatibility has not been verified. C12 private memory is available to callers that
inject a per-agent memory factory; see [C12](C12_MEMORY.md). C13 objective dataset
evaluation remains separate work. A file check must come from the trusted task caller or
evaluation definition, not from the Agent whose work is being assessed.
