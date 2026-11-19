"""Executable protocol fixture, never a production harness transport."""

import json
import os
import subprocess
import socket
import sys
import time
from pathlib import Path


def delegation_scenario(prompt):
    mode = prompt["goal"].splitlines()[0]
    suffix = "-fails" if mode.endswith("-fails") else ""
    mode = mode.removesuffix(suffix) if suffix else mode
    previous = prompt["completed_platform_calls"]
    with socket.socket(socket.AF_UNIX) as channel:
        channel.connect(os.environ["FAKE_MCP_SOCKET"])
        with channel.makefile("rb") as reader:
            def call(name, **arguments):
                request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}}
                channel.sendall(json.dumps(request).encode() + b"\n")
                result = json.loads(reader.readline())["result"]
                assert not result["isError"], result
                return json.loads(result["content"][0]["text"])

            if mode == "fixture-delegation-evidence":
                if not any(item["tool"] == "run_python" for item in previous):
                    call("write_file", path="check.py", purpose="code", content="print('checked')\n")
                    assert call("run_python", path="check.py")["status"] == "waiting_human"
                    return
                value = "unverifiable result" if suffix else "verified evidence"
                call("write_file", path="evidence.md", content=value)
            elif mode == "fixture-delegation-dependent":
                call("write_file", path="followup.md", content="review complete")
            else:
                if mode == "fixture-delegation-parent":
                    arguments = {"agent": "reviewer", "description": "fixture-delegation-review" + suffix,
                                 "path": "review.md", "contains": "Review: verified evidence"}
                    output, prefix = "report.md", "Analysis: "
                else:
                    assert mode == "fixture-delegation-review"
                    arguments = {"agent": "collector", "description": "fixture-delegation-evidence" + suffix,
                                 "path": "evidence.md", "contains": "verified evidence"}
                    output, prefix = "review.md", "Review: "
                replies = [item["outcome"] for item in previous if item["tool"] == "delegate"]
                result = call("delegate", **arguments)
                if not replies:
                    assert result["status"] == "waiting_dependency"
                    return
                assert result == replies[-1]
                artifact = result["result"]["artifact"]
                call("import_artifact", artifact_id=artifact["id"], destination=artifact["name"])
                content = call("read_file", path=artifact["name"])["result"]
                call("write_file", path=output, content=prefix + content)


def main():
    args = sys.argv[1:]
    session = args[args.index("--session-id") + 1]
    prompt = json.load(sys.stdin)
    mode = prompt["goal"]
    if mode == "hang":
        time.sleep(60)
        return 0
    if mode == "descendant":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path(os.environ["FAKE_CHILD_PID"]).write_text(str(child.pid))
        time.sleep(60)
        return 0
    if mode == "flood":
        while True:
            os.write(2, b"x" * 8192)
    if mode == "invalid":
        print("invalid JSON")
        return 0
    if mode == "missing":
        return 0
    if mode == "nonzero":
        print("sensitive diagnostic", file=sys.stderr)
        return 7
    if mode == "wrong_session":
        session = "other-session"
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session, "tools": [],
                      "mcp_servers": [{"name": "evo", "status": "connected"}]}), flush=True)
    if mode == "blocked_native":
        print(json.dumps({"type": "system", "subtype": "init", "session_id": session, "tools": ["Bash"]}), flush=True)
    elif mode == "failed_mcp":
        print(json.dumps({"type": "system", "subtype": "init", "session_id": session,
                          "mcp_servers": [{"name": "evo", "status": "failed"}]}), flush=True)
    else:
        if mode.startswith("fixture-delegation-"):
            delegation_scenario(prompt)
        if mode == "mcp":
            with socket.socket(socket.AF_UNIX) as channel:
                channel.connect(os.environ["FAKE_MCP_SOCKET"])
                with channel.makefile("rb") as reader:
                    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "write_file", "arguments": {"path": "fake.md", "content": "from MCP"}}}
                    invalid = {**call, "params": {"name": "write_file", "arguments": {"path": "booking.py", "purpose": "code"}}}
                    channel.sendall(json.dumps(invalid).encode() + b"\n")
                    rejection = json.loads(reader.readline())["result"]
                    assert rejection["isError"]
                    assert "Missing required fields: content" in rejection["content"][0]["text"]
                    channel.sendall(json.dumps(call).encode() + b"\n")
                    result = json.loads(reader.readline())
                    assert not result["result"]["isError"]
        print(json.dumps({"type": "assistant", "session_id": session,
                          "message": {"content": [{"type": "text", "text": "fixture response"}]}}), flush=True)
        print(json.dumps({"type": "result", "session_id": session, "is_error": mode == "error_result",
                          "result": "fixture-credential" if mode == "secret" else "fixture success"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
