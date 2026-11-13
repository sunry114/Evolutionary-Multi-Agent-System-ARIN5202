"""Isolated browser fixtures, never real Agent execution evidence."""
import sys
from pathlib import Path
from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.domain.models import Task
from evo.domain.statuses import TaskStatus

store = SQLiteStore(Path(sys.argv[1]) / "events.sqlite3")
for task_id, prompt in (("accept", "Accept sample"), ("revise", "Revise sample"),
                        ("error", "Error sample"),
                        ("xss", '<img src=x onerror="window.__xss=true">')):
    store.save_task(Task(task_id, prompt, owner="builder", status=TaskStatus.SUCCESS,
                         result='<script>window.__xss=true</script>'))
store.save_approval_request(request_id="approval", task_id="accept", agent="builder",
                            kind="approval", payload={"tool": "run_python"})
