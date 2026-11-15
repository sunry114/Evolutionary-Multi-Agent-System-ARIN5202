"""Durable run checkpoints and one active harness per task, including across processes."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import uuid
from dataclasses import asdict
from pathlib import Path

from evo.adapters.tools.filesystem import _directory, _read_at, _write_at
from evo.config import HarnessConfig
from evo.ports.harness import (ArtifactRef, HarnessCleanupError, HarnessError, HarnessEvent, ResumeRef,
                               RunHandle, RunRequest, RunResult, identifier)


def request_record(request):
    value = asdict(request)
    value["workspace"] = str(request.workspace)
    # Canonical JSON removes tuple/list differences after disk round trips.
    return json.loads(json.dumps(value))


class _Control:
    def __init__(self, record, lease):
        self.record = record
        self.lease = lease
        self.stop = threading.Event()
        self.done = threading.Event()
        self.lock = threading.RLock()
        self.operation_lock = threading.Lock()
        self.failure = ""


class ManagedHarness:
    adapter_name = "abstract"

    def __init__(self, state_root, broker, *, config=None):
        self.config = config or HarnessConfig(adapter=self.adapter_name)
        if self.config.adapter != self.adapter_name:
            raise HarnessError("Harness configuration selects a different adapter")
        self.broker = broker
        self.root = Path(os.path.abspath(state_root)) / "harness"
        if self.root.parent != broker.repository.path.parent.absolute():
            raise HarnessError("Harness and task repository must share the same state root")
        if self.root == broker.workspace.workspace or broker.workspace.workspace in self.root.parents:
            raise HarnessError("Harness state must be outside agent workspaces")
        with _directory(self.root, create=True):
            pass
        self._active = {}
        self._lock = threading.Lock()

    def capabilities(self):
        return {"adapter": self.adapter_name, "events": True, "cancel": True,
                "resume": "context_restart", "native_session_resume": False,
                "platform_approvals": True, "goal_verification": False}

    def _identity(self):
        return self.adapter_name

    def _clean(self, value):
        return value

    def _load(self, run_id):
        try:
            identifier(run_id)
            with _directory(self.root) as directory:
                record = json.loads(_read_at(directory, run_id + ".json", 2 * 1024 * 1024))
            if (record["adapter"] != self.adapter_name or record["request"]["agent"] != self.broker.executor.agent
                    or record["request"]["workspace"] != str(self.broker.workspace.workspace)):
                raise ValueError()
            return record
        except Exception:
            raise HarnessError("Harness checkpoint unavailable") from None

    def _save(self, control):
        data = json.dumps(self._clean(control.record), ensure_ascii=False).encode()
        with _directory(self.root) as directory:
            _write_at(directory, control.record["run_id"] + ".json", data, overwrite=True, limit=2 * 1024 * 1024)

    def _event(self, control, kind, payload=None):
        with control.lock:
            events = control.record["events"]
            if kind != "result" and (len(events) >= 511 or
                    len(json.dumps(control.record).encode()) + len(json.dumps(payload or {}).encode()) > 1536 * 1024):
                control.failure = "event_limit"
                control.stop.set()
                raise HarnessError("Harness event limit exceeded")
            events.append({"sequence": len(events) + 1, "kind": kind, "payload": self._clean(payload or {})})
            self._save(control)

    def start(self, request):
        return self._spawn(request)

    def _spawn(self, request, previous=None):
        if not isinstance(request, RunRequest):
            raise HarnessError("Invalid harness request")
        self.broker.validate(request)
        try:
            saved_request = request_record(request)
            if len(json.dumps(saved_request).encode()) > 128 * 1024:
                raise ValueError()
        except (ValueError, TypeError):
            raise HarnessError("Harness request is not bounded JSON context") from None
        # Lock by task, not adapter/agent/scope: selecting another adapter cannot
        # start a second executor for the same work item.
        with _directory(self.root) as directory:
            lease = os.open("task-" + request.task_id + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(lease)
            raise HarnessError("This task already has an active harness") from None
        try:
            with _directory(self.root) as directory:
                if "task-" + request.task_id + ".quarantine" in os.listdir(directory):
                    raise HarnessError("Task runtime cleanup needs operator reconciliation")
        except Exception:
            os.close(lease)
            raise
        agent_lease = None
        try:
            with _directory(self.root) as directory:
                agent_lease = os.open("agent-" + request.agent + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            fcntl.flock(agent_lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if agent_lease is not None:
                os.close(agent_lease)
            os.close(lease)
            raise HarnessError("This Agent workspace already has an active harness") from None
        run_id = uuid.uuid4().hex
        record = {"run_id": run_id, "adapter": self.adapter_name, "identity": self._identity(),
                  "request": saved_request, "native_session_id": str(uuid.uuid4()),
                  "events": [], "result": None, "pending": None, "inflight": None,
                  "context": [], "artifacts": [], "tool_calls": 0, "baseline_index": 0}
        if previous:
            for key in ("pending", "context", "artifacts", "tool_calls", "baseline_index"):
                record[key] = previous[key]
            record["parent_run_id"] = previous["run_id"]
        control = _Control(record, (lease, agent_lease))
        try:
            self._save(control)
            self.broker.repository.append_event(task_id=request.task_id, event_type="harness_started", actor=request.agent,
                                                payload={"run_id": run_id, "adapter": self.adapter_name,
                                                         "resume_strategy": "context_restart"})
            with self._lock:
                self._active[run_id] = control
            threading.Thread(target=self._worker, args=(control, request), daemon=True).start()
        except Exception:
            os.close(lease)
            os.close(agent_lease)
            raise
        return RunHandle(run_id)

    def resume(self, reference, request):
        previous = self._load(reference.run_id)
        if (asdict(reference) != {"run_id": previous["run_id"], "task_id": request.task_id,
                                  "agent": request.agent, "session_scope": request.session_scope}
                or previous["request"] != request_record(request)
                or previous["identity"] != self._identity()):
            raise HarnessError("Resume scope, request or backend does not match")
        # A lost process between side effect and checkpoint has an unknown outcome.
        # Never replay that tool automatically, including a consumed approval.
        if previous["inflight"]:
            raise HarnessError("Interrupted tool outcome is unknown; reconcile it before restarting")
        if previous["result"] and previous["result"]["error"] == "cleanup_failed":
            raise HarnessError("Runtime cleanup requires operator reconciliation")
        return self._spawn(request, previous)

    def events(self, handle, after=0):
        if type(after) is not int or after < 0:
            raise HarnessError("Invalid event cursor")
        record = self._load(handle.run_id)
        return tuple(HarnessEvent(**event) for event in record["events"] if event["sequence"] > after)

    @staticmethod
    def _result(value):
        if value is None:
            return None
        value = dict(value)
        value["artifacts"] = tuple(ArtifactRef(**x) for x in value["artifacts"])
        if value["resume_ref"]:
            value["resume_ref"] = ResumeRef(**value["resume_ref"])
        return RunResult(**value)

    def wait(self, handle, timeout=None):
        with self._lock:
            control = self._active.get(handle.run_id)
        if control is not None and not control.done.wait(timeout):
            return None
        return self._result(self._load(handle.run_id)["result"])

    def cancel(self, handle):
        with self._lock:
            control = self._active.get(handle.run_id)
        if control is not None:
            control.stop.set()
            if not control.done.wait(45):
                raise HarnessError("Harness cleanup still pending")
        result = self._result(self._load(handle.run_id)["result"])
        if result is None:
            raise HarnessError("Run is not owned by this process; inspect/reconcile before recovery")
        return result

    def _call(self, control, request, name, arguments, *, request_id=None):
        with control.operation_lock:
            if control.stop.is_set() or (control.record["pending"] and request_id is None):
                raise HarnessError("Harness is paused or stopping")
            with control.lock:
                if request_id is None:
                    control.record["tool_calls"] += 1
                    if control.record["tool_calls"] > self.config.max_tool_calls:
                        control.failure = "tool_limit"
                        control.stop.set()
                        raise HarnessError("Harness tool limit exceeded")
                if len(json.dumps(arguments).encode()) > 65536:
                    raise HarnessError("Platform tool arguments exceed harness limits")
                if len(json.dumps(control.record).encode()) > 1024 * 1024:
                    control.failure = "context_limit"
                    control.stop.set()
                    raise HarnessError("Harness checkpoint limit exceeded")
                control.record["inflight"] = {"name": name}
                self._event(control, "tool_call", {"name": name})
            try:
                result = self.broker.call(request.task_id, name, arguments, request_id=request_id)
            except Exception:
                control.failure = "platform_tool_error"
                control.stop.set()
                result = {"status": "failed", "error": "platform_tool_rejected"}
            encoded = json.dumps(self._clean(result), ensure_ascii=False)
            if len(encoded.encode()) > 16384:
                result = {"status": result.get("status", "failed"), "action_id": result.get("action_id"),
                          "output_truncated": True, "result": self._clean(encoded[:8192])}
            with control.lock:
                if not control.failure:
                    control.record["inflight"] = None
                if result.get("status") == "waiting_human":
                    control.record["pending"] = {"name": name, "arguments": arguments, "request_id": result["request_id"]}
                else:
                    control.record["pending"] = None
                    control.record["context"].append(self._clean({"tool": name, "arguments": arguments, "outcome": result}))
                    if name in ("write_file", "import_artifact") and result.get("status") == "success":
                        artifact = result["result"]
                        control.record["artifacts"] = [a for a in control.record["artifacts"] if a["path"] != artifact["path"]] + [artifact]
                self._event(control, "tool_result", {"name": name, "status": result.get("status"),
                                                       "request_id": result.get("request_id"), "action_id": result.get("action_id"),
                                                       "outcome": self._clean(result)})
            return self._clean(result)

    def _worker(self, control, request):
        status, text, error, exit_code = "failed", "", "harness_failed", None
        try:
            pending = control.record["pending"]
            if pending:
                outcome = self._call(control, request, **pending)
                if outcome["status"] == "waiting_human":
                    status, error = "waiting_human", ""
                elif outcome["status"] != "success":
                    status, error = "blocked" if outcome["status"] == "blocked" else "failed", "platform_action_stopped"
                else:
                    if self.adapter_name == "baseline":
                        control.record["baseline_index"] += 1
                    status, text, error, exit_code = self._execute(control, request)
            else:
                status, text, error, exit_code = self._execute(control, request)
        except HarnessCleanupError:
            control.failure = "cleanup_failed"
            status, error = "failed", "cleanup_failed"
            with _directory(self.root) as directory:
                _write_at(directory, "task-" + request.task_id + ".quarantine", b"Runtime cleanup unverified\n",
                          overwrite=True, limit=1024)
        except Exception:
            status, error = "failed", "harness_failed"
        finally:
            # MCP calls are serialized; a terminal checkpoint is never published
            # while a platform tool can still be producing side effects.
            with control.operation_lock, control.lock:
                if control.failure:
                    status, error = "failed", control.failure
                elif control.record["pending"]:
                    status, error = "waiting_human", ""
                elif control.stop.is_set():
                    status, error = ("failed", control.failure) if control.failure else ("cancelled", "cancelled")
                ref = ResumeRef(control.record["run_id"], request.task_id, request.agent, request.session_scope)
                result = RunResult(status, self._clean(text), error, control.record["native_session_id"],
                                   tuple(ArtifactRef(**x) for x in control.record["artifacts"]), ref, exit_code)
                control.record["result"] = asdict(result)
                try:
                    self._event(control, "result", {"status": status, "error": error})
                    try:
                        self.broker.repository.append_event(task_id=request.task_id, event_type="harness_finished", actor=request.agent,
                                                            payload={"run_id": control.record["run_id"], "status": status})
                    except Exception:
                        control.record["result"]["status"] = "failed"
                        control.record["result"]["error"] = "audit_failed"
                        self._save(control)
                finally:
                    for lease in control.lease:
                        os.close(lease)
                    control.done.set()
                    with self._lock:
                        self._active.pop(control.record["run_id"], None)

    def _execute(self, control, request):
        raise NotImplementedError
