"""Claude Code hooks for native host work orders."""

import json
import os
import re
import shlex
import sys
from pathlib import Path

from evo.adapters.tools.web import WebLimits, WebToolError, WebTools


_HTTP_FAILURE = re.compile(r"^\s*The server returned HTTP ([45]\d\d)\b", re.I)
_TRANSIENT_FETCH = re.compile(
    r"\b(?:socket (?:is )?closed|connection (?:reset|closed)|ECONNRESET|ETIMEDOUT|timed out|network error|"
    r"proxy connection ended before receiving CONNECT response)\b",
    re.I,
)


def webfetch_status(text):
    match = _HTTP_FAILURE.match(text or "")
    return int(match[1]) if match else None


def settings():
    hook = {"type": "command", "command": shlex.join([sys.executable, str(Path(__file__).resolve())]),
            "timeout": 11}
    return {"env": {"ENABLE_TOOL_SEARCH": "false"}, "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [hook]}],
        "PostToolUse": [{"matcher": "WebFetch", "hooks": [hook]}],
        "PostToolUseFailure": [{"matcher": "WebFetch", "hooks": [hook]}],
    }}


def has_project_shell_rules(path):
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        permissions = config.get("permissions") or {}
        hooks = config.get("hooks") or {}
        return bool(permissions.get("deny") or permissions.get("ask") or config.get("sandbox")
                    or hooks.get("PreToolUse"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError, AttributeError):
        return True


def update_input(event, arguments):
    command = arguments.get("command")
    if event.get("tool_name") != "Bash" or not isinstance(command, str) or not re.search(r"[;|\n]", command):
        return None
    # Claude checks permissions against rewritten input. Keep the original command
    # when project or system settings can apply their own shell rules.
    roots = {Path(event.get("cwd") or Path.cwd()), Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())}
    managed = Path("/etc/claude-code")
    if (managed / "managed-settings.json").is_file() or (managed / "managed-settings.d").is_dir() or any(
        has_project_shell_rules(root / ".claude" / filename)
        for root in roots
        for filename in ("settings.json", "settings.local.json")
    ):
        return None
    command = shlex.join(["bash", "-eo", "pipefail", "-c", command])
    return {"updatedInput": dict(arguments, command=command)}


def fetch_context(event, arguments):
    response = event.get("tool_response", event.get("error", ""))
    if isinstance(response, dict):
        response = response.get("text") or response.get("result") or response.get("error") or ""
    if not isinstance(response, str):
        return None
    status = webfetch_status(response)
    if status in {401, 403, 404, 410, 429, 521}:
        return {"additionalContext": f"WebFetch failed (HTTP {status}). Choose a usable public source; "
                                     "do not repeat the identical request."}
    failed = event.get("hook_event_name") == "PostToolUseFailure"
    if not (status in {408, 502, 503, 504} or (failed and _TRANSIENT_FETCH.search(response))):
        return None
    url = arguments.get("url")
    if event.get("is_interrupt") or not isinstance(url, str):
        return None
    try:
        page = WebTools("webfetch-fallback", limits=WebLimits(timeout=7)).fetch_url(url)
    except WebToolError as error:
        return {"additionalContext": f"The bounded public HTTPS fallback also failed ({error}). "
                                     "Use another accessible source."}
    text = page.text[:14000]
    if len(page.text) > len(text):
        text += "\n[Remaining page text omitted]"
    return {"additionalContext": f"WebFetch connection failed. Evo retrieved this page via public HTTPS. "
                                 f"Source: {page.url}\n{text}"}


def run_hook(event):
    name = event.get("hook_event_name")
    arguments = event.get("tool_input") or {}
    if name == "PreToolUse":
        result = update_input(event, arguments)
    elif name in {"PostToolUse", "PostToolUseFailure"} and event.get("tool_name") == "WebFetch":
        result = fetch_context(event, arguments)
    else:
        result = None
    if result:
        return {"hookSpecificOutput": dict(result, hookEventName=name)}
    return {}


if __name__ == "__main__":
    print(json.dumps(run_hook(json.load(sys.stdin)), ensure_ascii=False))
