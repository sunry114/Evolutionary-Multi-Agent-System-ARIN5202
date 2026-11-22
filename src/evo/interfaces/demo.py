"""Volatile preview, not a task executor or persistence substitute."""
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4
from evo.application.chat import parse_group_chat
from evo.application.issues import parse_issue_title
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
        self.chats = {}
        self.groups = {}

    def snapshot(self):
        with self.lock:
            return deepcopy({key: value for key, value in self.data.items() if key != "messages"})

    def dispatch(self, operation, payload):
        with self.lock:
            if operation == "create":
                if set(payload) - {"title", "prompt", "agent", "squad", "checks", "no_memory", "from_group"}:
                    raise ValueError("Invalid issue fields")
                title = parse_issue_title(payload)
                prompt = payload.get("prompt", "").strip()
                if not prompt:
                    raise ValueError("Prompt required")
                task = {"id": uuid4().hex, "title": title, "prompt": prompt, "status": "created",
                        "assignee_type": None, "assignee_id": None, "routing_mode": None}
                if "squad" in payload or "agent" in payload:
                    self.assign(task, payload)
                task.update(lane="todo", current_task_id=task["id"])
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
                        "lane": record["lane"], "next_action": next_action, "approvals": [], "negotiations": [],
                        "logs": [], "artifacts": [], "messages": [
                            deepcopy(row) for row in self.data["messages"] if row.get("task_id") == task["id"]]}
            if operation == "tasks":
                return {"tasks": [deepcopy(row) for row in self.data["tasks"]
                                  if not payload.get("owner") or (row["assignee_id"] == payload["owner"] and
                                      payload.get("owner_type", row["assignee_type"]) == row["assignee_type"])]}
            if operation == "chat-threads":
                return {key: deepcopy(sorted(records.values(), key=lambda chat: chat["updated_at"], reverse=True))
                        for key, records in (("chats", self.chats), ("groups", self.groups))}
            if operation == "new-group":
                name, members = parse_group_chat(payload, self.data["agents"])
                now = datetime.now(timezone.utc).isoformat()
                group = {"id": uuid4().hex, "name": name, "members": members, "created_at": now, "updated_at": now}
                self.groups[group["id"]] = group
                return {"group": deepcopy(group)}
            if operation == "new-chat":
                agent = payload.get("agent")
                if agent not in self.data["agents"]:
                    raise ValueError("Unknown agent")
                now = datetime.now(timezone.utc).isoformat()
                chat = {"id": uuid4().hex, "agent": agent, "title": None, "created_at": now, "updated_at": now}
                self.chats[chat["id"]] = chat
                return {"chat": deepcopy(chat)}
            if operation in ("dm", "group"):
                chat_id = payload.get("chat_id")
                chat = self.chats.get(chat_id)
                if chat_id is not None and chat is None:
                    raise ValueError("Chat not found")
                group_id = payload.get("group_id")
                group = self.groups.get(group_id)
                if group_id is not None and (group is None or operation != "group" or "task_id" in payload):
                    raise ValueError("Invalid group chat")
                content = payload.get("message", "").strip()
                if not content:
                    raise ValueError("Message required")
                now = datetime.now(timezone.utc).isoformat()
                message = {"id": uuid4().hex, "kind": operation, "recipient": chat["agent"] if chat else payload.get("agent"),
                           "content": content, "sender": "human", "task_id": payload.get("task_id"),
                           "chat_id": chat_id, "group_id": group_id, "created_at": now}
                self.data["messages"].append(message)
                if chat:
                    chat["title"] = chat["title"] or content
                    chat["updated_at"] = now
                if group:
                    group["updated_at"] = now
                return {"status": "blocked", "message": "Message preview saved; no model called"}
            if operation == "memory":
                return {"memory": [], "mode": "demo"}
            if operation == "messages":
                rows = [row for row in self.data["messages"] if not row.get("task_id")]
                if "chat_id" in payload:
                    rows = [row for row in rows if row.get("chat_id") == payload["chat_id"]]
                elif "group_id" in payload:
                    if payload["group_id"] not in self.groups:
                        raise ValueError("Group chat not found")
                    rows = [row for row in rows if row.get("group_id") == payload["group_id"]]
                elif payload.get("scope") == "group":
                    rows = [row for row in rows if row["kind"] == "group" and not row.get("group_id")]
                elif "agent" in payload:
                    rows = [row for row in rows if not row.get("chat_id") and row.get("recipient") == payload["agent"]]
                else:
                    rows = [row for row in rows if not row.get("group_id")]
                return {"messages": deepcopy(rows)}
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
