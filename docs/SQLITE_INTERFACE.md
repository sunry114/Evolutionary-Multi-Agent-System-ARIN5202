# C14 SQLite workspace integration

Latest C14 milestone (2026-11-15): ChatService-backed DM/group chat, persisted
history and ApprovalService answers are now connected. See [C14_STATUS.md](C14_STATUS.md)
for current validation, limitations and remaining C11/C12/C13 dependencies.
Earlier dated counts and unavailable-chat statements below are historical.

Updated: 2026-11-13 (Asia/Shanghai). Based on main cbac88e (C03 and C05 merged).

## Implemented behavior

With no EVO_APPLICATION_FACTORY and no --demo, CLI/Web now load SQLiteApplication.
It uses B's SQLiteStore and A's ReviewRepository port through D's ReviewService.
Runtime database: <state>/events.sqlite3; --state defaults to ignored .evo/.

Supported operations: tasks, task, trace, review, revision and (since C10) answer. Reads return stored
records; they do not create tasks. Review keeps technical status unchanged.
Revision atomically links one created child; it does not execute or schedule it.
Concurrent duplicate revisions conflict, with no extra child or dirty audit event.

Since C10, pending approval/clarification requests can be answered through CLI/Web.
Decisions are atomic and durable; denial blocks the task. Answering does not
automatically execute a tool or resume a model loop. The current branch
connects run/resume/chat/eval. Memory and metrics routes remain
unavailable; HTTP returns 503 and CLI returns 2 for those operations.
The existing --demo mode stays volatile; EVO_APPLICATION_FACTORY overrides SQLite
mode for the future full application.

## Run against existing records

After installation:

```bash
python -m evo --state .evo web
python -m evo --state .evo tasks --limit 100
python -m evo --state .evo task TASK_ID
python -m evo --state .evo trace TASK_ID
python -m evo --state .evo review TASK_ID accepted
# Alternative for another successful, pending-review task:
python -m evo --state .evo review OTHER_TASK_ID changes_requested --feedback 'Fix output'
python -m evo --state .evo revision OTHER_TASK_ID 'Apply feedback'
```

Open http://127.0.0.1:8768. A new database shows an empty workspace, with no sample
successes or generated Agent results. Task records must come from the real backend;
the tests explicitly seed isolated fixtures. Use the same --state directory for
CLI and Web. Global flags precede the command.

The snapshot lists up to 100 recent tasks and 100 pending requests. CLI tasks
accepts limit 1-500; use task TASK_ID to inspect an older known record, including
its linked children. This is a bounded local workspace view, not full pagination.
An interrupted running task is shown as stored; it is never automatically
restarted. DB persistence alone does not provide durable execution/approval resume.

## Validation

10 new integration tests in tests/integration/test_sqlite_interfaces.py cover:

- Default SQLite loader and custom factory override.
- CLI decisions, linked revision and trace across separate processes.
- Duplicate decision/revision rejection without dirty records.
- Two concurrent revisions producing exactly one child.
- Invalid types, limits and IDs without unintended writes.
- Unavailable backends without side effects.
- Persisted tasks and pending approval snapshot (C10 additionally tests human answers).
- Bounded listing and querying a technically failed task.
- Actual Web server process restart preserving review, revision and trace;
  the old CSRF token is rejected and a duplicate revision remains rejected.
- HTTP invalid/conflicting writes (409) and unavailable execution (503).

Local full regression: 66 tests, 64 passed, 2 POSIX-only checks skipped on Windows
with Python 3.12.7. No real API key or outbound model request is needed.
Browser interaction and documented HTTP boundary coverage were completed in the
2026-11-14 follow-up; see [WEB_ACCEPTANCE.md](WEB_ACCEPTANCE.md). Full-backend
full execution/chat flows are still outside this milestone. C10 adds approval
decision persistence, schema/CSRF rejection and CLI/Web answer tests; see [C10](C10_HITL_GIT.md).

The wheel must include migrations/*.sql. CI now initializes SQLiteApplication from
the installed wheel, in addition to checking Web assets; editable installs alone
would not catch missing migration resources.

## Team boundary

Only two read queries were added to SQLiteStore: list_tasks and
list_pending_requests. No schema change or replacement persistence implementation.
B's atomic review/revision transactions remain authoritative. Future execution
integration must schedule the created child through A, and approval actions/resume
must go through C/A rather than direct UI database writes.
