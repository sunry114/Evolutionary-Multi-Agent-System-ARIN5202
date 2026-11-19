# Team discussion and private human replies

The group view is a read-only transcript of agent collaboration. Creating a task
starts a background discussion through `discuss`; the existing synchronous
`group` API remains available for CLI/integration callers. Every agent gets an
initial turn unless it already has a pending human question. A question pauses
that agent, while independent agents continue speaking. Directed `@agent`
follow-ups are bounded, and waiting agents are not invoked again automatically.

Each saved team has one coordinator: prefer `lead`, then `coordinator`, then
`manager`, otherwise the first registered role. Ordinary questions from workers
are redirected to this coordinator in the group transcript. After the team's
initial turns, the coordinator resolves questions from context or consolidates
unresolved questions into its private inbox. Only this role may create human
chat questions. Pending updates retain the original worker identities, and after
the human responds the coordinator shares the relevant decision and wakes those
workers. Planning and delivery-format questions also use the coordinator.
On server restart, existing pending text questions are rerouted to the saved
team's coordinator; action approvals keep their original owners.

Human questions are stored atomically as a private DM and a `chat_question`
clarification request. These conversation requests have no response deadline.
Models return a separate `human_question` containing only the question for the
human. Group discussion stays in `reply`. Legacy mixed replies are filtered at
read time so private question cards and DM history show only `@human` sections;
the original public transcript remains available in the group view.
Every agent may independently request critical-action approval through its bound
platform tools. These cards retain the requesting agent, exact action and scope,
and expose **批准 / 拒绝** buttons, not a text answer field. Approvals retain
their existing expiration policy and are not forwarded through the coordinator.
Answering a
question atomically saves the private response and consumes the request once,
then starts a background turn for the coordinator. That agent shares a
task-relevant decision with the team. Raw private answers are not inserted in
the group transcript or another agent's context. As with any model-generated
summary, its wording is produced by the model.

The frontend uses one refresh loop every second for workspace state and the
selected task's committed messages, including during human typing and background
model calls. Board cards and the group summary derive their phase from the same
task snapshot: discussion, waiting for questions/approvals, ready for execution,
execution, and terminal results. Child approval/question counts roll up to the
root task. This is incremental message delivery, not token streaming.

Selection is shared by the board, group and private views. The selected board
card is highlighted, and the group summary provides the same start, continue
and terminate controls plus a return-to-board button. Termination and clearing
update both views; clearing the selected task also clears its selection and
visible chat. External task creation is discovered even without a selected or
executing task. Stale refresh responses are discarded after selection changes.
Unchanged state does not rebuild the UI. Changed state preserves request drafts,
cursor selection, expanded task details and loaded trace/metrics. The group view
has no human reply input.

Tasks created after this change retain their agent registry snapshot. Legacy
drafts without a snapshot use the current registry. Conversations are serialized
per task, allowing independent tasks and task controls to remain responsive.
Execution waits for pending human questions and active background discussions;
planning receives the task's recent public agent decisions, not raw private DMs.

Discussion start, completion and failure are recorded in the task event log.
An interrupted/failed discussion can be retried from the group view; previously
saved messages and private answers remain available after restart. Background
threads are process-local and do not automatically restart after a server exit.
Retrying an initial discussion can repeat initial turns; retrying a completed
private-answer turn is rejected. This is a local SQLite implementation, not a
distributed worker queue.

## Design references

The implementation was written for this project's Python/SQLite boundaries;
no Multica source code was copied. The following mechanisms were inspected at
Multica revision `ffe80875eb002ab21db7f73b5a171be995bc9fff`:

- [Scoped events and private delivery](https://github.com/multica-ai/multica/blob/ffe80875eb002ab21db7f73b5a171be995bc9fff/server/internal/events/bus.go).
- [Durable inbox writes followed by delivery](https://github.com/multica-ai/multica/blob/ffe80875eb002ab21db7f73b5a171be995bc9fff/server/internal/inbox/deliver.go).
- [Conversation-bound wakeups](https://github.com/multica-ai/multica/blob/ffe80875eb002ab21db7f73b5a171be995bc9fff/server/internal/service/chat_wakeup_dispatch.go).

## Verification

`python -m unittest discover -s tests -q` covers persistence, private answers,
expiry policies, role snapshots, routing, planning context and HTTP behavior.
With Playwright and Chromium installed, run
`python tests/browser/chat_collaboration.py` to verify incremental display while
later agents are blocked, all four team members, private answering, draft
preservation across refresh and JavaScript errors. The browser test uses a
controlled provider and makes no real model requests.
