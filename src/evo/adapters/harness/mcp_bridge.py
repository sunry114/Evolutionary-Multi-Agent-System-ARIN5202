"""Bound platform tools and a bounded MCP stdio-over-Unix-socket bridge."""

from __future__ import annotations

import hashlib
import json
import socketserver
import threading
from dataclasses import asdict, is_dataclass

from evo.ports.harness import HarnessError, HarnessCleanupError
from evo.adapters.tools.filesystem import PathPolicyError, PURPOSE_SUFFIXES
from evo.adapters.tools.web import WebToolError
from evo.ports.tools import ToolError, ToolCleanupError
from evo.application.executor import ActionAlreadyCompleted, ActionPreparationError


class ToolCallRejected(HarnessError):
    """A bounded, recoverable tool error that the agent may correct."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


S = {"type": "string"}
PURPOSE = {"type": "string", "enum": ["document", "code", "config", "data"]}
SCHEMAS = {
    "read_file": schema({"path": S, "purpose": PURPOSE, "start_line": {"type": "integer"},
                         "max_lines": {"type": "integer"}}, ("path",)),
    "edit_file": schema({"path": S, "purpose": PURPOSE, "old_text": S, "new_text": S},
                        ("path", "old_text", "new_text")),
    "search_files": schema({"query": S, "path": S, "max_results": {"type": "integer"}}, ("query",)),
    "check_python": schema({"path": S}, ("path",)),
    "write_file": schema({"path": S, "content": S, "purpose": PURPOSE,
                          "overwrite": {"type": "boolean"}, "append": {"type": "boolean"}}, ("path", "content")),
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

    def __init__(self, executor, *, web=None, artifacts=None, delegation=None):
        self.executor = executor
        self.workspace = executor.workspace
        self.repository = executor.repository
        self.web = web
        self.artifacts = artifacts
        self.delegation = delegation

    def validate(self, request):
        task = self.repository.get_task(request.task_id)
        if (request.agent != self.executor.agent or task["owner"] != request.agent
                or request.workspace != self.workspace.workspace
                or task["status"] not in ("running", "waiting_human")):
            raise HarnessError("Harness request does not match the bound task/workspace")

    def tools(self):
        names = set(SCHEMAS)
        if self.web is None:
            names -= {"search", "fetch_url"}
        if self.artifacts is None:
            names -= {"publish_artifact", "list_artifacts", "import_artifact"}
        if self.delegation is None:
            names -= {"delegate"}
        descriptions = {
            "read_file": "Read UTF-8 text. start_line is 1-based; max_lines (1-500) reads a range. "
                         "Use ranges for large files; use purpose:code for source files.",
            "edit_file": "Replace exactly one occurrence of old_text with new_text atomically. "
                         "Read the target first. An absent or ambiguous match changes nothing. Use this to repair code "
                         "without rewriting or emptying an existing file; use purpose:code for source files.",
            "search_files": "Search literal text in workspace files recursively; returns paths, purposes, "
                            "1-based line numbers and excerpts. Bounded to 128 files and 50 matches.",
            "check_python": "Check .py syntax without executing or importing code. Returns valid and error "
                            "line/offset; this is not a runtime or functional test. No execution approval needed.",
            "write_file": "Write UTF-8 text. Allowed suffixes by purpose: " +
                          json.dumps({key: sorted(value) for key, value in PURPOSE_SUFFIXES.items()}) +
                          ". Parent directories are created automatically. Creates files, does not execute code. "
                          "For large files, first write a small initial chunk, then append:true adds the next "
                          "chunk verbatim (no automatic newline). Omit append or use append:false to replace. "
                          "Keep each chunk under 2000 characters; never resend the entire accumulated file.",
            "run_python": "Run an existing .py script in the isolated Python container after human approval. "
                          "Python standard library only; no Node, npm, shell, package installation or external network.",
        }
        return [{"name": name, "description": descriptions.get(name, "Platform " + name + "; critical actions wait for human approval."),
                 "inputSchema": SCHEMAS[name]} for name in sorted(names)]

    def call(self, task_id, name, arguments, *, request_id=None):
        allowed = {x["name"] for x in self.tools()}
        if name not in allowed or not isinstance(arguments, dict):
            raise ToolCallRejected("invalid_tool")
        spec = SCHEMAS[name]
        if set(arguments) - set(spec["properties"]) or set(spec["required"]) - set(arguments):
            raise ToolCallRejected("invalid_arguments")
        for key, value in arguments.items():
            rule = spec["properties"][key]
            typ = rule["type"]
            if ((typ == "string" and not isinstance(value, str))
                    or (typ == "boolean" and type(value) is not bool)
                    or (typ == "integer" and type(value) is not int)
                    or (typ == "array" and (not isinstance(value, list) or len(value) > 64
                                            or any(not isinstance(x, str) for x in value)))
                    or ("enum" in rule and value not in rule["enum"])):
                raise ToolCallRejected("invalid_arguments")
        task = self.repository.get_task(task_id)
        if task["owner"] != self.executor.agent:
            raise HarnessError("Tool task ownership mismatch")
        if name in CRITICAL_DISPATCH:
            try:
                return self.executor.execute(task_id, name, arguments, request_id=request_id)
            except ActionAlreadyCompleted as completed:
                if request_id is not None:
                    # An old approval checkpoint is not a new model proposal.
                    # Keep explicit resume replays blocked rather than advancing
                    # a stale action sequence from a consumed approval.
                    raise ToolCallRejected("action_already_completed") from None
                action = completed.action
                return {"status": action["status"], "action_id": action["id"],
                        "result": action["result"], "replayed": True,
                        "message": "This exact approved action already completed. Returning its recorded "
                                   "result; no tool executed again and no approval was consumed. "
                                   "For Python, changed workspace inputs require a new approval."}
            except ActionPreparationError as error:
                # Only preparation is recoverable here. Approval scope errors
                # and failures after execution must retain their stop behavior.
                raise ToolCallRejected(error.code) from None
            except ToolCleanupError:
                raise HarnessCleanupError("Tool runtime cleanup unverified") from None
        if request_id is not None or task["status"] != "running":
            raise HarnessError("Task is not ready for platform tools")
        # Content is private workspace data, not durable audit metadata.
        summary = {key: value for key, value in arguments.items()
                   if key not in ("content", "old_text", "new_text", "query", "url")}
        action_id = self.repository.start_action(task_id, self.executor.agent, name, summary)
        try:
            if name in ("read_file", "write_file", "edit_file", "search_files", "check_python", "list_files"):
                value = getattr(self.workspace, name)(**arguments)
                if name in ("write_file", "edit_file"):
                    content = self.workspace.read_file(arguments["path"], purpose=arguments.get("purpose", "document"))
                    value = {"path": arguments["path"], "purpose": arguments.get("purpose", "document"),
                             "sha256": hashlib.sha256(content.encode()).hexdigest()}
            elif name in ("search", "fetch_url"):
                value = getattr(self.web, name)(**arguments)
            elif name == "delegate":
                value = self.delegation(task_id, **arguments)
            else:
                method = {"publish_artifact": "publish", "list_artifacts": "list_available", "import_artifact": "import_artifact"}[name]
                value = getattr(self.artifacts, method)(**arguments)
                if name == "import_artifact":
                    value = {"path": arguments["destination"], "purpose": value.purpose, "sha256": value.sha256}
            status = "success"
            if name == "delegate" and value["status"] != "success":
                # A child approval cannot be replayed as a fresh delegation.
                # Surface a stopped parent instead of hiding the child failure.
                status = "blocked" if value["status"] in ("blocked", "waiting_human") else "failed"
            self.repository.finish_action(action_id, succeeded=status == "success",
                                          result={"status": status},
                                          fail_task_on_error=name == "delegate")
            return {"status": status, "action_id": action_id, "result": jsonable(value)}
        except (PathPolicyError, WebToolError, ToolError, ValueError, TypeError,
                PermissionError, OSError, KeyError) as error:
            self.repository.finish_action(action_id, succeeded=False, result={"error": "tool_failed"},
                                          fail_task_on_error=False)
            code = ("web_tool_failed" if isinstance(error, WebToolError) else
                    "path_rejected" if isinstance(error, PathPolicyError) else "tool_failed")
            if name == "edit_file" and isinstance(error, ValueError) and not isinstance(error, PathPolicyError):
                code = "edit_rejected"
            raise ToolCallRejected(code) from None
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
                        self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
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
