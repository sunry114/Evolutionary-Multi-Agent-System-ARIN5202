"""Volatile preview, not a task executor or persistence substitute."""
from copy import deepcopy
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
                           "content": payload.get("message", ""), "sender": "human"}
                self.data["messages"].append(message)
                return {"status": "blocked", "message": "Message preview saved; no model called"}
            if operation == "memory":
                return {"memory": [], "mode": "demo"}
            if operation == "trace":
                return {"events": [], "mode": "demo"}
            if operation == "metrics":
                return {"status": "unavailable", "message": "No evaluation in demo mode"}
            raise ValueError("Operation requires the real application backend")
