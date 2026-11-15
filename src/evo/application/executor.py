"""C10 single-action executor. C11 owns the model loop and DAG scheduling."""

import hashlib
from dataclasses import asdict
from typing import Mapping

from evo.domain.policies import CRITICAL_TOOLS
from evo.ports.tools import ExecutionResult, ToolError


class ActionExecutor:
    """Bind one trusted task owner to its ports; model arguments never grant approval."""

    def __init__(self, repository, approval, *, agent, workspace, python, git,
                 require_commit_approval=False):
        if workspace.agent != agent or type(require_commit_approval) is not bool:
            raise ValueError("Invalid trusted executor configuration")
        self.repository = repository
        self.approval = approval
        self.agent = agent
        self.workspace = workspace
        self.python = python
        self.git = git
        self.require_commit_approval = require_commit_approval

    def _prepare(self, name, arguments):
        if not isinstance(arguments, Mapping):
            raise ValueError("Tool arguments must be an object")
        expected = {"run_python": {"path"}, "delete_file": {"path", "purpose"},
                    "git_init": set(), "git_status": set(),
                    "git_commit": {"paths", "message"}, "git_push": {"repository", "branch"}}
        if name not in expected:
            raise ValueError("Unsupported C10 action")
        args = dict(arguments)
        if name == "delete_file":
            args.setdefault("purpose", "document")
        if set(args) != expected[name]:
            raise ValueError("Unexpected or missing tool arguments")
        payload = {"arguments": args}
        if name in ("run_python", "delete_file"):
            purpose = "code" if name == "run_python" else args["purpose"]
            content = self.workspace.read_file(args["path"], purpose=purpose)
            payload["sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if name == "git_commit":
            self.git.validate_commit(args["paths"], args["message"])
            payload["sha256"] = {}
            for path in args["paths"]:
                # The Git port validates paths; hash through the workspace port.
                suffix = path.rsplit(".", 1)[-1].lower()
                purpose = {"py": "code", "json": "data", "csv": "data", "yaml": "config", "yml": "config"}.get(suffix, "document")
                content = self.workspace.read_file(path, purpose=purpose)
                payload["sha256"][path] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if name == "git_push":
            self.git.validate_push_target(args["repository"], args["branch"])
            payload["revision"] = self.git.head()
        return payload

    def execute(self, task_id, name, arguments, *, request_id=None):
        task = self.repository.get_task(task_id)
        if task["owner"] != self.agent:
            raise ValueError("Task owner does not match executor")
        if task["status"] == "blocked":
            return {"status": "blocked"}
        if task["status"] not in ("running", "waiting_human"):
            raise ValueError("Task is not executable")
        payload = self._prepare(name, arguments)
        critical = name in CRITICAL_TOOLS or (name == "git_commit" and self.require_commit_approval)
        if critical:
            if request_id is None:
                request_id = self.approval.request_approval(task_id, self.agent, name, payload)
            self.approval.check_scope(request_id, task_id, self.agent, name, payload)
            decision = self.approval.get_answer(request_id)
            if decision is None:
                return {"status": "waiting_human", "request_id": request_id}
            if decision["status"] != "approved":
                return {"status": "blocked", "request_id": request_id}
            if not self.approval.claim(request_id, task_id, self.agent, name, payload):
                return {"status": "blocked", "request_id": request_id}
            action_id = request_id
        else:
            if request_id is not None:
                raise ValueError("This action does not consume approval")
            action_id = self.repository.start_action(task_id, self.agent, name, payload)
        args = payload["arguments"]
        try:
            if name == "run_python":
                result = self.python.run_python(args["path"], approved=True)
            elif name == "delete_file":
                self.workspace.delete_file(args["path"], purpose=args["purpose"], approved=True)
                result = None
            elif name == "git_init":
                result = self.git.init()
            elif name == "git_status":
                result = self.git.status()
            elif name == "git_commit":
                result = self.git.commit(args["paths"], args["message"])
            else:
                result = self.git.push(args["repository"], args["branch"], payload["revision"], approved=True)
            if isinstance(result, ExecutionResult):
                summary = {"exit_code": result.exit_code, "timed_out": result.timed_out,
                           "output_limited": result.output_limited}
                succeeded = result.exit_code == 0 and not result.timed_out and not result.output_limited
            elif name == "delete_file" and result is None:
                summary, succeeded = {}, True
            else:
                raise ToolError("Invalid tool execution result")
        except (ToolError, PermissionError, ValueError, OSError):
            summary, succeeded = {"error": "tool_failed"}, False
            result = None
        except Exception:
            self.repository.finish_action(action_id, succeeded=False, result={"error": "tool_failed"})
            raise ToolError("Action execution failed") from None
        self.repository.finish_action(action_id, succeeded=succeeded, result=summary)
        return {"status": "success" if succeeded else "failed", "action_id": action_id,
                "result": asdict(result) if isinstance(result, ExecutionResult) else result}
