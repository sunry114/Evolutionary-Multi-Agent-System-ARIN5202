"""Explicit real CLI smoke; development endpoints never become evaluation evidence."""

import argparse
import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from evo.adapters.harness import create_harness
from evo.adapters.harness.backend import local_claude_backend
from evo.adapters.llm.credentials import load_key
from evo.adapters.harness.mcp_bridge import PlatformToolBroker
from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.adapters.tools.docker import DockerPythonRunner
from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.adapters.tools.git import DockerGitTools
from evo.application.approvals import ApprovalService
from evo.application.executor import ActionExecutor
from evo.config import HarnessConfig, HarnessModelConfig, ProviderConfig
from evo.domain.models import Task
from evo.domain.statuses import TaskStatus
from evo.ports.harness import RunRequest


SMOKE_CODE = "print('C10A_SMOKE_OK')\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    backend = parser.add_mutually_exclusive_group(required=True)
    backend.add_argument("--development", action="store_true")
    backend.add_argument("--hkgai", action="store_true", help="Use permitted HKGAI settings and private credential")
    parser.add_argument("--local-claude-settings", type=Path)
    parser.add_argument("--approve-smoke-code", action="store_true",
                        help="Pre-approve only the exact fixed smoke.py content in this isolated smoke task")
    parser.add_argument("--state", type=Path, default=Path(".evo/harness-smoke"))
    parser.add_argument("--cli", default="claude")
    args = parser.parse_args()
    if args.hkgai:
        if args.local_claude_settings:
            parser.error("Local Claude settings require --development")
        provider = ProviderConfig.from_environment()
        if provider.provider != "hkgai":
            parser.error("--hkgai requires EVO_PROVIDER=hkgai")
        model = HarnessModelConfig(provider="hkgai", endpoint=provider.base_url, model=provider.model)
        credential_loader = lambda: load_key("hkgai")
    else:
        model, credential_loader = local_claude_backend(args.local_claude_settings, development=args.development)
    state = args.state.absolute()
    store = SQLiteStore(state / "events.sqlite3")
    task_id = "smoke-" + uuid.uuid4().hex
    workspace = WorkspaceFilesystem(state, task_id)
    store.save_task(Task(task_id, "C10a explicit live smoke", owner=task_id, status=TaskStatus.RUNNING))
    approval = ApprovalService(store)
    executor = ActionExecutor(store, approval, agent=task_id, workspace=workspace,
                              python=DockerPythonRunner(workspace), git=DockerGitTools(workspace))
    broker = PlatformToolBroker(executor)
    adapter = create_harness(HarnessConfig(adapter="claude_code", cli_path=args.cli), state, broker,
                             model=model, credential_loader=credential_loader)
    request = RunRequest(task_id, task_id, "installation-smoke", workspace.workspace,
                         "Use the evo platform tools to write hello.md containing exactly C10A_FILE_OK and a newline; "
                         "write smoke.py with exactly this Python source: " + repr(SMOKE_CODE) + ". "
                         "Read hello.md to verify it, then run smoke.py using run_python. "
                         "If completed_platform_calls already records successful run_python, do not run it again. "
                         "Finish after all these actions. No other files or tools are needed.")
    handle = adapter.start(request)
    initial_run_id = handle.run_id
    deadline = time.monotonic() + 240
    result = None
    try:
        while time.monotonic() < deadline:
            result = adapter.wait(handle, timeout=1)
            if result is None:
                continue
            if result.status != "waiting_human":
                break
            pending = store.list_pending_requests()
            if not args.approve_smoke_code:
                break
            if len(pending) != 1:
                raise RuntimeError("Unexpected smoke approval requests")
            item = pending[0]
            if (item["task_id"] != task_id or item["agent"] != task_id or item["action"] != "run_python"
                    or item["payload"]["arguments"] != {"path": "smoke.py"}
                    or workspace.read_file("smoke.py", purpose="code") != SMOKE_CODE):
                raise RuntimeError("Smoke request exceeds the explicit code pre-approval")
            approval.answer(item["id"], {"approved": True})
            handle = adapter.resume(result.resume_ref, request)
        else:
            result = adapter.cancel(handle)
    except BaseException:
        adapter.cancel(handle)
        raise
    code_actions = [store.get_action(event["payload"]["action_id"])
                    for event in store.list_events(task_id)
                    if event["event_type"] == "action_started" and event["payload"].get("tool_name") == "run_python"]
    code_outputs = [event.payload.get("outcome", {}).get("result", {})
                    for event in adapter.events(handle)
                    if event.kind == "tool_result" and event.payload.get("name") == "run_python"]
    def read(path, purpose="document"):
        try:
            return workspace.read_file(path, purpose=purpose)
        except (OSError, ValueError):
            return None
    checks = {"file_generated": read("hello.md") == "C10A_FILE_OK\n",
              "exact_code_content": read("smoke.py", "code") == SMOKE_CODE,
              "one_successful_code_action": len(code_actions) == 1 and code_actions[0]["status"] == "success"
                  and code_actions[0]["result"].get("exit_code") == 0,
              "verified_code_stdout": len(code_outputs) == 1 and code_outputs[0].get("stdout") == "C10A_SMOKE_OK\n",
              "resumed_after_approval": handle.run_id != initial_run_id}
    passed = result is not None and result.status == "success" and all(checks.values())
    report = {"passed": passed, "status": result.status if result else "unknown", "error": result.error if result else "",
              "task_id": task_id, "run_id": handle.run_id, "cli_version": adapter.config.cli_version,
              "backend": "hkgai_chat_protocol" if args.hkgai else "explicit_local_development",
              "formal_evaluation": False,
              "initial_run_id": initial_run_id, "checks": checks,
              "artifacts": [asdict(x) for x in result.artifacts] if result else []}
    print(json.dumps(report, indent=2))
    (state / "latest-smoke.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
