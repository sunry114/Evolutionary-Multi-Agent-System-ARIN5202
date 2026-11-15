"""Isolated browser fixtures, never real Agent execution evidence."""
import sys
from pathlib import Path
from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.domain.models import Task
from evo.domain.statuses import TaskStatus
from evo.application.approvals import ApprovalService

store = SQLiteStore(Path(sys.argv[1]) / "events.sqlite3")
for task_id, prompt in (("accept", "Accept sample"), ("revise", "Revise sample"),
                        ("error", "Error sample"),
                        ("xss", '<img src=x onerror="window.__xss=true">')):
    store.save_task(Task(task_id, prompt, owner="builder", status=TaskStatus.SUCCESS,
                         result='<script>window.__xss=true</script>'))
store.save_task(Task("critical", "Critical action fixture", owner="builder", status=TaskStatus.RUNNING))
ApprovalService(store).request_approval("critical", "builder", "delete_file",
                                      {"arguments": {"path": "report.md"}, "sha256": "fixture"})

store.save_task(Task("denied", "Denied action fixture", owner="builder", status=TaskStatus.RUNNING))
ApprovalService(store).request_approval("denied", "builder", "run_python", {"fixture": True})
store.save_task(Task("clarify", "Clarification fixture", owner="builder", status=TaskStatus.RUNNING))
ApprovalService(store).request_clarification("clarify", "Which input file?")
