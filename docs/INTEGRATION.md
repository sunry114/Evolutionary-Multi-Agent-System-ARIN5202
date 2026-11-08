# Member D integration contract

This branch was built against an empty code repository. The following interfaces
are explicit proposals for A/B/C to review, not claims that their work is present.

Set EVO_APPLICATION_FACTORY=your_package.bootstrap:create_application.
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
state. Production trace/review restart recovery and pending approval recovery
require B/C implementations and integration tests. Do not present preview records
as evaluation evidence.


## LLM integration after the API follow-up
A can import evo.adapters.llm.factory.create_provider instead of separately
constructing HTTP clients. It reads the documented non-secret environment config.
The verified HKGAI completion endpoint is /v1/chat/completions. Catch ProviderError
and persist only its safe code, not third-party raw response bodies. CredentialError
indicates missing/unsafe credentials. Credentials remain outside ApplicationGateway
snapshots, task payloads, trace and reports. check-provider is connectivity evidence
only; A still owns the agent loop and B the objective evaluation.
