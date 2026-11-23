"""Executable protocol fixture, never a production harness transport."""

import json
import os
import subprocess
import socket
import sys
import time
from pathlib import Path


INCIDENT_REPORT = "Analyze the supplied test-log.txt and deliver report.md."
INCIDENT_REVIEW = "Independently review the supplied test-log.txt."


def incident_scenario(mode, previous, call):
    if mode == INCIDENT_REVIEW:
        log = call("read_file", path="test-log.txt", purpose="document")["result"]
        assert all(line in log for line in ("L02", "free_bytes=0", "L03", "error=ENOSPC",
                                            "L05", "free_bytes=67108864", "L07", "status=PASS"))
        call("write_file", path="review.md", purpose="document", content=(
            "# Independent review\n\n"
            "L02–L04 show that run A17 had zero free bytes on export-temp, hit ENOSPC, and failed. "
            "L05 shows cleanup restored 67,108,864 bytes. L06–L07 show that run A18 passed.\n\n"
            "Independent review conclusion: The same test passed after temporary space was freed, "
            "but one rerun does not establish that the failure will stay resolved.\n"))
        return

    deliveries = [item["outcome"] for item in previous if item["tool"] == "delegate"]
    if not deliveries:
        log = call("read_file", path="test-log.txt", purpose="document")["result"]
        assert "error=ENOSPC" in log and "status=FAIL" in log
        result = call("delegate", agent="reviewer", path="review.md", contains="Independent review conclusion:",
                      description=INCIDENT_REVIEW + "\nCheck the failure, cleanup, rerun, and evidence limits. "
                      "Return review.md with line citations and the label 'Independent review conclusion:'.")
        assert result["status"] == "waiting_dependency"
        return

    artifact = deliveries[0]["result"]["artifact"]
    call("import_artifact", artifact_id=artifact["id"], destination="review.md")
    review = call("read_file", path="review.md", purpose="document")["result"]
    conclusion = next(line for line in review.splitlines() if line.startswith("Independent review conclusion:"))
    call("write_file", path="report.md", purpose="document", content=(
        "# CSV export test failure\n\n## Root cause\n\n"
        "Run A17 ran out of space on export-temp (L02). The temporary write failed with ENOSPC (L03), "
        "and export_writes_csv failed (L04). Cleanup restored 67,108,864 bytes (L05).\n\n"
        "## Independent verification\n\n> " + conclusion + "\n\n"
        "The same test passed once in run A18 (L06–L07). The log records neither the source of disk "
        "growth nor additional runs under sustained load. Check temporary-file retention and available space.\n"))


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

            if mode in (INCIDENT_REPORT, INCIDENT_REVIEW):
                incident_scenario(mode, previous, call)
                return
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
    session_flag = "--resume" if "--resume" in args else "--session-id"
    session = args[args.index(session_flag) + 1]
    prompt = json.load(sys.stdin)
    if "--input-format" in args:
        assert prompt["type"] == "user" and prompt["message"]["role"] == "user"
        prompt = json.loads(prompt["message"]["content"])
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
    builtins = ["Bash", "Read", "Write"] if mode.startswith("native_issue") else []
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session, "tools": builtins,
                      "mcp_servers": [{"name": "evo", "status": "connected"}]}), flush=True)
    if mode == "native_issue_api_error":
        error = "API Error: 401 fixture-credential authentication rejected"
        print(json.dumps({"type": "system", "subtype": "api_retry", "session_id": session,
                          "error": "authentication_failed"}))
        print(json.dumps({"type": "assistant", "session_id": session, "is_api_error_message": True,
                          "message": {"content": [{"type": "text", "text": error}]}}))
        print(json.dumps({"type": "result", "subtype": "success", "session_id": session,
                          "is_error": True, "result": error}))
        return 1
    if mode == "native_issue_result_errors":
        print(json.dumps({"type": "result", "subtype": "error_during_execution", "session_id": session,
                          "is_error": True, "errors": ["Connection closed by the model API", "Request ID: fixture"]}))
        return 1
    if mode == "native_issue_stderr":
        sys.stderr.write("x" * 10000 + "\nUnable to connect with fixture-credential\n")
        return 1
    if mode == "native_issue_api_timeout":
        print(json.dumps({"type": "system", "subtype": "api_retry", "session_id": session,
                          "error": "connection_error"}), flush=True)
        time.sleep(60)
        return 1
    if mode == "blocked_native":
        print(json.dumps({"type": "system", "subtype": "init", "session_id": session, "tools": ["Bash"]}), flush=True)
    elif mode == "failed_mcp":
        print(json.dumps({"type": "system", "subtype": "init", "session_id": session,
                          "mcp_servers": [{"name": "evo", "status": "failed"}]}), flush=True)
    else:
        if mode in ("large_stream", "oversized_event"):
            if mode == "large_stream":
                for _ in range(1025):
                    os.write(2, b"x" * 8192)
            event = json.dumps({"type": "assistant", "session_id": session, "message": {"content": [
                {"type": "text", "text": "x" * 17000}]}})
            for _ in range(520 if mode == "large_stream" else 1):
                print(event)
        if mode == "system_status_updates":
            status = json.dumps({"type": "system", "subtype": "status", "session_id": session})
            for _ in range(520):
                print(status)
        if builtins:
            for item in prompt.get("previous_deliverables", []):
                assert Path(item["path"]).read_text() == "Previous output"
            tools = ("Bash", "Write", "Read")
            if mode == "native_issue_many_tools":
                tools *= 8
            elif mode == "native_issue_webfetch_http_error":
                tools = ("WebFetch",)
            for index, name in enumerate(tools):
                tool_id = "test_" + str(index)
                print(json.dumps({"type": "assistant", "session_id": session, "message": {"content": [
                    {"type": "tool_use", "name": name, "id": tool_id, "input": {}}]}}), flush=True)
                output = "fixture result"
                if mode == "native_issue_long_tool_output" and name == "Bash":
                    output = [{"type": "text", "text": "fixture-credential " + "长" * 5000 + "tail"}]
                elif mode == "native_issue_webfetch_http_error":
                    output = "The server returned HTTP 403 Forbidden."
                print(json.dumps({"type": "user", "session_id": session, "message": {"content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": output,
                     "is_error": mode == "native_issue_many_tools" and index == 0}]}}), flush=True)
        if mode == "native_issue_tool_limit":
            with socket.socket(socket.AF_UNIX) as channel:
                channel.connect(os.environ["FAKE_MCP_SOCKET"])
                with channel.makefile("rb") as reader:
                    for index, (name, arguments) in enumerate((
                        ("search", {"query": "fixture"}),
                        ("fetch_url", {"url": "https://example.com/page"}),
                        ("fetch_url", {"url": "https://example.com/page"}),
                    )):
                        call = {"jsonrpc": "2.0", "id": index, "method": "tools/call",
                                "params": {"name": name, "arguments": arguments}}
                        channel.sendall(json.dumps(call).encode() + b"\n")
                        result = json.loads(reader.readline())["result"]
                        assert result["isError"] == (index > 0)
                        if index > 0:
                            assert json.loads(result["content"][0]["text"])["error"] == "tool_limit"
        if mode.startswith("fixture-delegation-") or mode.partition("\n")[0] in (INCIDENT_REPORT, INCIDENT_REVIEW):
            delegation_scenario(prompt)
        if mode in ("mcp", "missing_git_repository", "large_mcp"):
            with socket.socket(socket.AF_UNIX) as channel:
                channel.connect(os.environ["FAKE_MCP_SOCKET"])
                with channel.makefile("rb") as reader:
                    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "write_file", "arguments": {"path": "fake.md", "content": "from MCP"}}}
                    if mode == "large_mcp":
                        call["params"] = {"name": "read_file", "arguments": {"path": "large.md"}}
                    if mode == "missing_git_repository":
                        status = {**call, "params": {"name": "git_status"}}
                        channel.sendall(json.dumps(status).encode() + b"\n")
                        probe = json.loads(reader.readline())["result"]
                        assert probe["isError"]
                        assert json.loads(probe["content"][0]["text"])["error"] == "git_repository_missing"
                    invalid = {**call, "params": {"name": "write_file", "arguments": {"path": "booking.py", "purpose": "code"}}}
                    channel.sendall(json.dumps(invalid).encode() + b"\n")
                    rejection = json.loads(reader.readline())["result"]
                    assert rejection["isError"]
                    assert "Missing required fields: content" in rejection["content"][0]["text"]
                    channel.sendall(json.dumps(call).encode() + b"\n")
                    result = json.loads(reader.readline())
                    assert not result["result"]["isError"]
                    if mode == "large_mcp":
                        value = json.loads(result["result"]["content"][0]["text"])["result"]
                        assert value == "a" * 65536 + "end of tool result"
        print(json.dumps({"type": "assistant", "session_id": session,
                          "message": {"content": [{"type": "text", "text": "fixture response"}]}}), flush=True)
        result = "fixture-credential" if mode == "secret" else "fixture success"
        if mode == "native_issue_many_tools":
            result = "x" * 10000 + "\nThe first tool failed. The remaining steps finished."
        print(json.dumps({"type": "result", "session_id": session, "is_error": mode == "error_result",
                          "result": result}), flush=True)
        if builtins:
            outputs = Path(prompt["deliverables_directory"])
            if mode == "native_issue_output_symlink":
                (outputs / "linked.md").symlink_to(outputs.parent / "outside.md")
            else:
                (outputs / "hello-world.md").write_text("# Hello World\n")
        if mode == "native_issue_failed_with_output":
            return 7
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
