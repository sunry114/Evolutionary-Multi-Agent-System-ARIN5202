"""Bound platform tools and a bounded MCP stdio-over-Unix-socket bridge."""

from __future__ import annotations

import hashlib
import json
import socketserver
import threading
from dataclasses import asdict, is_dataclass

from evo.ports.harness import HarnessError, ToolInputError

from .container_client import MCP_RESPONSE_BYTES


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


S = {"type": "string"}
PURPOSE = {"type": "string", "enum": ["document", "code", "config", "data", "slides"]}
SCHEMAS = {
    "read_file": schema({"path": S, "purpose": PURPOSE}, ("path",)),
    "write_file": schema({"path": S, "content": {"type": "string", "description": "Required: complete UTF-8 file contents to write."}, "purpose": PURPOSE, "overwrite": {"type": "boolean"}}, ("path", "content")),
    "list_files": schema({"path": S}),
    "run_python": schema({"path": S}, ("path",)),
    "delete_file": schema({"path": S, "purpose": PURPOSE}, ("path",)),
    "git_init": schema({}), "git_status": schema({}),
    "git_commit": schema({"paths": {"type": "array", "items": S}, "message": S}, ("paths", "message")),
    "git_push": schema({"repository": S, "branch": S}, ("repository", "branch")),
    "search": schema({"query": S}, ("query",)),
    "fetch_url": schema({"url": S}, ("url",)),
    "publish_artifact": schema({"path": S, "recipients": {"type": "array", "items": S}, "purpose": PURPOSE}, ("path", "recipients")),
    "list_artifacts": schema({}),
    "import_artifact": schema({"artifact_id": S, "destination": S}, ("artifact_id", "destination")),
    "delegate": schema({"agent": S, "description": S, "path": S, "contains": S},
                       ("agent", "description", "path", "contains")),
    "post_issue_comment": schema({"content": S}, ("content",)),
    "set_issue_status": schema({"status": {"type": "string", "enum": ["in_progress", "in_review", "blocked"]}},
                               ("status",)),
}
CRITICAL_DISPATCH = frozenset(("run_python", "delete_file", "git_init", "git_status", "git_commit", "git_push"))


def jsonable(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (list, tuple)):
        return [jsonable(x) for x in value]
    return value


class PlatformToolBroker:
    """Constructed by trusted application code, bound to exactly one agent."""

    def __init__(self, executor, *, web=None, artifacts=None, delegation=None,
                 issue_comment=None, issue_status=None, allow_approval_tools=True, issue_controls_only=False):
        self.executor = executor
        self.workspace = executor.workspace
        self.repository = executor.repository
        self.web = web
        self.artifacts = artifacts
        self.delegation = delegation
        self.issue_comment = issue_comment
        self.issue_status = issue_status
        self.allow_approval_tools = allow_approval_tools
        self.issue_controls_only = issue_controls_only

    def validate(self, request):
        task = self.repository.get_task(request.task_id)
        if (request.agent != self.executor.agent or task["owner"] != request.agent
                or request.workspace != self.workspace.workspace
                or task["status"] not in ("running", "waiting_human")):
            raise HarnessError("Harness request does not match the bound task/workspace")

    def tools(self):
        names = {"post_issue_comment", "set_issue_status"} if self.issue_controls_only else set(SCHEMAS)
        if self.web is None:
            names -= {"search", "fetch_url"}
        if self.artifacts is None:
            names -= {"publish_artifact", "list_artifacts", "import_artifact"}
        if self.delegation is None:
            names -= {"delegate"}
        if self.issue_comment is None:
            names.discard("post_issue_comment")
        if self.issue_status is None:
            names.discard("set_issue_status")
        if not self.allow_approval_tools:
            from evo.domain.policies import CRITICAL_TOOLS
            names -= CRITICAL_TOOLS
        descriptions = {"git_status": "Inspect an initialized Git repository. Use list_files for ordinary workspace files.",
                        "post_issue_comment": "Post on this issue. Use @agent to request a separate run.",
                        "set_issue_status": "Set the assigned issue's status. Review requests apply after successful execution and archiving. Read issue_status for the current status."}
        return [{"name": name, "description": descriptions.get(name, "Platform " + name),
                 "inputSchema": SCHEMAS[name]} for name in sorted(names)]

    def call(self, task_id, name, arguments, *, request_id=None):
        allowed = {x["name"] for x in self.tools()}
        if name not in allowed or not isinstance(arguments, dict):
            raise HarnessError("Unsupported platform tool")
        spec = SCHEMAS[name]
        missing = sorted(set(spec["required"]) - set(arguments))
        if missing:
            raise ToolInputError("Missing required fields: " + ", ".join(missing))
        if set(arguments) - set(spec["properties"]):
            raise ToolInputError("Unexpected field; use only the declared tool parameters")
        for key, value in arguments.items():
            rule = spec["properties"][key]
            typ = rule["type"]
            if ((typ == "string" and not isinstance(value, str))
                    or (typ == "boolean" and type(value) is not bool)
                    or (typ == "array" and (not isinstance(value, list) or len(value) > 64
                                            or any(not isinstance(x, str) for x in value)))
                    or ("enum" in rule and value not in rule["enum"])):
                raise ToolInputError("Invalid value for declared field: " + key)
        task = self.repository.get_task(task_id)
        if task["owner"] != self.executor.agent:
            raise HarnessError("Tool task ownership mismatch")
        if name == "delegate":
            return self.delegation(task_id, self.executor.agent, **arguments, request_id=request_id)
        if name in CRITICAL_DISPATCH:
            return self.executor.execute(task_id, name, arguments, request_id=request_id)
        if request_id is not None or task["status"] != "running":
            raise HarnessError("Task is not ready for platform tools")
        if name == "post_issue_comment":
            return {"status": "success", "result": self.issue_comment(**arguments)}
        if name == "set_issue_status":
            return {"status": "success", "result": self.issue_status(**arguments)}
        # Content is private workspace data, not durable audit metadata.
        summary = {key: value for key, value in arguments.items() if key not in ("content", "query", "url")}
        action_id = self.repository.start_action(task_id, self.executor.agent, name, summary)
        try:
            if name in ("read_file", "write_file", "list_files"):
                value = getattr(self.workspace, name)(**arguments)
                if name == "write_file":
                    content = self.workspace.read_file(arguments["path"], purpose=arguments.get("purpose", "document"))
                    value = {"path": arguments["path"], "purpose": arguments.get("purpose", "document"),
                             "sha256": hashlib.sha256(content.encode()).hexdigest()}
            elif name in ("search", "fetch_url"):
                value = getattr(self.web, name)(**arguments)
            else:
                method = {"publish_artifact": "publish", "list_artifacts": "list_available", "import_artifact": "import_artifact"}[name]
                value = getattr(self.artifacts, method)(**arguments)
                if name == "publish_artifact":
                    self.repository.append_event(task_id=task_id, event_type="artifact_published",
                                                 actor=self.executor.agent, payload={
                                                     "artifact": asdict(value), "path": arguments["path"],
                                                     "purpose": value.purpose})
                elif name == "import_artifact":
                    value = {"path": arguments["destination"], "purpose": value.purpose, "sha256": value.sha256}
            self.repository.finish_action(action_id, succeeded=True, result={"status": "success"})
            return {"status": "success", "action_id": action_id, "result": jsonable(value)}
        except Exception:
            self.repository.finish_action(action_id, succeeded=False, result={"error": "tool_failed"})
            raise HarnessError("Platform tool failed") from None


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address):
        pass


class MCPBridge:
    def __init__(self, path, broker, call):
        bridge = self
        self.lock = threading.Lock()

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(610)
                while True:
                    try:
                        line = self.rfile.readline(2 * 1024 * 1024 + 1)
                    except (OSError, TimeoutError):
                        break
                    if not line:
                        break
                    if len(line) > 2 * 1024 * 1024:
                        break
                    try:
                        response = bridge.dispatch(json.loads(line), broker, call)
                    except Exception:
                        response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid MCP request"}}
                    if response is not None:
                        encoded = json.dumps(response, ensure_ascii=False).encode() + b"\n"
                        if len(encoded) > MCP_RESPONSE_BYTES:
                            encoded = json.dumps({
                                "jsonrpc": "2.0", "id": response["id"],
                                "error": {"code": -32000, "message": "MCP response exceeds its message limit"},
                            }).encode() + b"\n"
                        self.wfile.write(encoded)
                        self.wfile.flush()

        self.server = _Server(str(path), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def dispatch(self, message, broker, call):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError()
        if "id" not in message:
            return None
        ident = message["id"]
        if type(ident) not in (str, int):
            raise ValueError()
        method = message.get("method")
        params = message.get("params", {})
        response = {"jsonrpc": "2.0", "id": ident}
        if method in ("initialize", "server/discover"):
            version = params.get("protocolVersion", "2025-06-18")
            if version not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"):
                version = "2025-06-18"
            response["result"] = {"protocolVersion": version, "capabilities": {"tools": {}},
                                  "serverInfo": {"name": "evo-platform", "version": "1.0.0"}}
        elif method == "ping":
            response["result"] = {}
        elif method == "tools/list":
            response["result"] = {"tools": broker.tools()}
        elif method == "tools/call":
            try:
                with self.lock:
                    result = call(params["name"], params.get("arguments", {}))
                response["result"] = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                                      "isError": result.get("status") in ("failed", "blocked")}
            except Exception:
                response["result"] = {"content": [{"type": "text", "text": "Platform tool rejected"}], "isError": True}
        else:
            response["error"] = {"code": -32601, "message": "Unknown MCP method"}
        return response

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
