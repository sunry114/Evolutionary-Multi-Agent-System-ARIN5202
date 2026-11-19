"""Executable protocol fixture, never a production harness transport."""

import json
import os
import subprocess
import socket
import sys
import time
from pathlib import Path


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
    if mode in ("system_noise", "many_thinking_tokens"):
        for _ in range(40 if mode == "system_noise" else 1024):
            print(json.dumps({"type": "system", "subtype": "thinking_tokens", "session_id": session}), flush=True)
    if mode == "blocked_native":
        print(json.dumps({"type": "system", "subtype": "init", "session_id": session, "tools": ["Bash"]}), flush=True)
    elif mode == "failed_mcp":
        print(json.dumps({"type": "system", "subtype": "init", "session_id": session,
                          "mcp_servers": [{"name": "evo", "status": "failed"}]}), flush=True)
    else:
        if mode == "model_unavailable":
            print(json.dumps({"type": "assistant", "session_id": session,
                              "message": {"content": [{"type": "text", "text":
                                  "There's an issue with the selected model (fixture-model). "
                                  "It may not exist or you may not have access to it."}]}}), flush=True)
            return 1
        if mode == "mcp":
            with socket.socket(socket.AF_UNIX) as channel:
                channel.connect(os.environ["FAKE_MCP_SOCKET"])
                with channel.makefile("rb") as reader:
                    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "write_file", "arguments": {"path": "fake.md", "content": "from MCP"}}}
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
