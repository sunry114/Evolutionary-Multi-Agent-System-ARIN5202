"""Volatile preview, not a task executor or persistence substitute."""
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4


class DemoApplication:
    def __init__(self):
        self.lock = RLock()
        self.data = {"mode": "demo (in-memory; no agents execute)",
                     "agents": ["researcher", "builder", "reviewer"],
                     "tasks": [], "messages": [], "memory": [], "requests": []}

    def snapshot(self):
        with self.lock:
            return deepcopy(self.data)

    def dispatch(self, operation, payload):
        with self.lock:
            if operation == "create":
                prompt = payload.get("prompt", "").strip()
                if not prompt:
                    raise ValueError("Prompt required")
                task = {"id": uuid4().hex, "prompt": prompt, "status": "created",
                        "review_status": None, "result": "Preview only: backend not connected"}
                self.data["tasks"].append(task)
                return {"status": "created", "task_id": task["id"]}
            if operation == "terminate":
                task = next((item for item in self.data["tasks"] if item["id"] == payload.get("task_id")), None)
                if task is None or task["status"] != "created":
                    raise ValueError("Task cannot be terminated")
                task["status"] = "blocked"
                task["result"] = "Terminated by human"
                before = len(self.data["messages"])
                self.data["messages"] = [message for message in self.data["messages"]
                                         if message.get("task_id") != task["id"]]
                return {"status": "blocked", "task_id": task["id"],
                        "cleared_messages": before - len(self.data["messages"])}
            if operation == "clear_finished":
                if payload:
                    raise ValueError("Invalid clear request")
                before = len(self.data["tasks"])
                self.data["tasks"] = [task for task in self.data["tasks"]
                                      if task["status"] not in ("success", "failed", "blocked")]
                return {"cleared_tasks": before - len(self.data["tasks"])}
            if operation == "run":
                prompt = payload.get("prompt", "").strip()
                if not prompt:
                    raise ValueError("Prompt required")
                task = {"id": uuid4().hex, "prompt": prompt, "status": "blocked",
                        "review_status": None, "result": "Preview only: backend not connected"}
                self.data["tasks"].append(task)
                return deepcopy(task)
            if operation in ("dm", "group"):
                message = {"kind": operation, "agent": payload.get("agent", "group"),
                           "content": payload.get("message", ""), "sender": "human",
                           "task_id": payload.get("task_id"),
                           "created_at": datetime.now(timezone.utc).isoformat()}
                self.data["messages"].append(message)
                return {"status": "blocked", "message": "Message preview saved; no model called"}
            if operation == "memory":
                return {"memory": [], "mode": "demo"}
            if operation == "trace":
                return {"events": [], "mode": "demo"}
            if operation == "metrics":
                return {"status": "unavailable", "message": "No evaluation in demo mode"}
            raise ValueError("Operation requires the real application backend")
