"""Issue-centric read model for the CLI and workbench."""

from uuid import uuid4

from evo.domain.models import Message


def issue_lane(task):
    if task["status"] == "created":
        return "draft"
    if task["status"] == "waiting_human":
        return "needs_action"
    if task["status"] in ("failed", "blocked"):
        return "blocked"
    if task["status"] == "success":
        return {"pending": "review", "changes_requested": "changes", "revising": "changes",
                "accepted": "done"}.get(task.get("review_status"), "done")
    return "in_progress"


class IssueService:
    def __init__(self, store):
        self.store = store

    def list(self, limit=100, *, owner=None, owner_type=None, squad_name=None):
        issues = self.store.list_issues(limit, owner=owner, owner_type=owner_type, squad_name=squad_name)
        return [{**row, "lane": issue_lane(row)} for row in issues]

    @staticmethod
    def _next_action(current, approvals):
        if current["status"] == "waiting_human":
            return "answer" if any(request["actionable"] for request in approvals) else "resume"
        if current["status"] == "created":
            return "start" if current["assignee_id"] else "assign"
        return {"review": "review", "changes": "revision"}.get(issue_lane(current))

    def detail(self, task_id, *, squad_name=None):
        issue_id, tasks, events, messages, approvals = self.store.issue_records(task_id)
        if squad_name is not None:
            for task in tasks:
                if task["assignee_type"] == "squad":
                    task["assignee_id"] = squad_name
        by_id = {task["id"]: task for task in tasks}
        issue = by_id[issue_id]
        runs = [issue]
        while runs[-1]["revision_task_id"]:
            runs.append(by_id[runs[-1]["revision_task_id"]])
        current = runs[-1]
        for request in approvals:
            request["actionable"] = (request["status"] == "pending" and
                                     by_id[request["task_id"]]["status"] in ("running", "planning", "waiting_human"))
        negotiations = []
        logs = []
        artifacts = []
        for event in events:
            kind = event["event_type"]
            if kind.startswith("assignment_"):
                negotiations.append(event)
            elif kind not in ("issue_config", "run_config", "memory_context"):
                logs.append(event)
            if kind in ("objective_verified", "artifact_published"):
                evidence = event["payload"]
                artifacts.append({"event_id": event["id"], "task_id": event["task_id"],
                                  "owner": by_id[event["task_id"]]["owner"], "path": evidence["path"],
                                  "purpose": evidence["purpose"], "archive": evidence.get("artifact"),
                                  "verified": kind == "objective_verified"})
        return {
            "issue": issue, "task": by_id[task_id], "current": current, "runs": runs, "tree": tasks,
            "children": [task for task in tasks if task["parent_id"] == task_id],
            "lane": issue_lane(current), "next_action": self._next_action(current, approvals),
            "messages": [message for message in messages if message["kind"] in ("comment", "group", "agent")],
            "approvals": approvals, "negotiations": negotiations, "logs": logs, "artifacts": artifacts,
        }

    def comment(self, task_id, content):
        issue_id = self.store.issue_id(task_id)
        message = Message(uuid4().hex, "comment", "human", content, task_id=issue_id)
        self.store.save_message(message)
        return {"issue_id": issue_id, "message_id": message.id}
