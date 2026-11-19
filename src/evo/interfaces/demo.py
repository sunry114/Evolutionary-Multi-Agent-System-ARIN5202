"""Volatile preview, not a task executor or persistence substitute."""
from copy import deepcopy
from threading import RLock
from uuid import uuid4
from evo.application.issues import issue_lane
from evo.domain.routing import issue_route


class DemoApplication:
    def __init__(self):
        self.lock = RLock()
        self.data = {"mode": "demo (in-memory; no agents execute)",
                     "agents": ["researcher", "builder", "reviewer"],
                     "squad": {"name": "default", "lead": "researcher",
                               "members": ["researcher", "builder", "reviewer"]},
                     "capabilities": {"configure": False, "eval": False},
                     "tasks": [], "messages": []}

    def snapshot(self):
        with self.lock:
            return deepcopy({key: value for key, value in self.data.items() if key != "messages"})

    def dispatch(self, operation, payload):
        with self.lock:
            if operation == "create":
                if set(payload) - {"prompt", "agent", "squad", "checks", "no_memory", "from_group"}:
                    raise ValueError("Invalid issue fields")
                prompt = payload.get("prompt", "").strip()
                if not prompt:
                    raise ValueError("Prompt required")
                task = {"id": uuid4().hex, "prompt": prompt, "status": "created",
                        "assignee_type": None, "assignee_id": None, "routing_mode": None}
                if "squad" in payload or "agent" in payload:
                    self.assign(task, payload)
                task.update(lane="draft", current_task_id=task["id"])
                self.data["tasks"].append(task)
                return {"status": "created", "task": deepcopy(task)}
            if operation in ("start", "task", "assign", "comment"):
                task = next((task for task in self.data["tasks"] if task["id"] == payload["task_id"]), None)
                if task is None:
                    raise ValueError("Task not found")
                if operation == "start":
                    if not task["assignee_id"]:
                        raise ValueError("Assign the issue before starting")
                    task.update(status="blocked", lane="blocked", result="预览模式无法执行工单")
                    return {"status": task["status"], "task": deepcopy(task)}
                if operation == "assign":
                    if set(payload) - {"task_id", "agent", "squad"}:
                        raise ValueError("Invalid assignment fields")
                    self.assign(task, payload)
                    return deepcopy(task)
                if operation == "comment":
                    self.data["messages"].append({"task_id": task["id"], "kind": "comment", "sender": "human",
                                                 "content": payload["message"]})
                    return {"issue_id": task["id"]}
                record = deepcopy(task)
                next_action = ("start" if task["assignee_id"] else "assign") if record["status"] == "created" else None
                return {"task": record, "issue": record, "current": record, "runs": [record], "tree": [record],
                        "lane": issue_lane(record), "next_action": next_action, "approvals": [], "negotiations": [],
                        "logs": [], "artifacts": [], "messages": [
                            deepcopy(row) for row in self.data["messages"] if row.get("task_id") == task["id"]]}
            if operation == "tasks":
                return {"tasks": [deepcopy(row) for row in self.data["tasks"]
                                  if not payload.get("owner") or (row["assignee_id"] == payload["owner"] and
                                      payload.get("owner_type", row["assignee_type"]) == row["assignee_type"])]}
            if operation in ("dm", "group"):
                message = {"kind": operation, "agent": payload.get("agent", "group"),
                           "content": payload.get("message", ""), "sender": "human", "task_id": payload.get("task_id")}
                self.data["messages"].append(message)
                return {"status": "blocked", "message": "Message preview saved; no model called"}
            if operation == "memory":
                return {"memory": [], "mode": "demo"}
            if operation == "messages":
                return {"messages": [deepcopy(row) for row in self.data["messages"] if not row.get("task_id")]}
            if operation == "trace":
                return {"events": [], "mode": "demo"}
            if operation == "metrics":
                return {"status": "unavailable", "message": "No evaluation in demo mode"}
            raise ValueError("Operation requires the real application backend")

    @staticmethod
    def assign(task, payload):
        if task["status"] != "created":
            raise ValueError("Only drafts can be assigned")
        recipients = {"agent", "squad"} & payload.keys()
        if len(recipients) != 1:
            raise ValueError("Choose one agent or Squad")
        assignee_type = recipients.pop()
        mode = issue_route(assignee_type)
        task.update(assignee_type=assignee_type, assignee_id=payload[assignee_type], routing_mode=mode.value)
