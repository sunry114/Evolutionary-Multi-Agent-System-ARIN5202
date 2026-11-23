import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evo.adapters.harness.native_hooks import run_hook, settings, webfetch_status
from evo.ports.tools import WebPage


class NativeHookTests(unittest.TestCase):
    def test_compound_bash_propagates_an_earlier_failed_pipeline(self):
        command = run_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                            "tool_input": {"command": "false | cat; echo masked"}})
        argv = command["hookSpecificOutput"]["updatedInput"]["command"]
        result = subprocess.run(["bash", "-c", argv], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("masked", result.stdout)

    def test_bash_hooks_leave_commands_intact_when_a_project_has_permission_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".claude"
            config.mkdir()
            (config / "settings.json").write_text('{"permissions":{"deny":["Bash(false)"]}}')
            result = run_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": directory,
                               "tool_input": {"command": "false | cat; echo masked"}})
        self.assertEqual(result, {})

    def test_search_schema_is_sent_upfront(self):
        self.assertEqual(settings()["env"]["ENABLE_TOOL_SEARCH"], "false")

    def test_native_webfetch_connection_failure_uses_bounded_public_tool(self):
        url = "https://public.example.net/report"
        with patch("evo.adapters.harness.native_hooks.WebTools") as web:
            web.return_value.fetch_url.return_value = WebPage(url, "[UNTRUSTED WEB CONTENT]\nReport", "text/html")
            result = run_hook({"hook_event_name": "PostToolUseFailure", "tool_name": "WebFetch",
                               "tool_input": {"url": url}, "error": "Socket is closed"})
        self.assertLess(web.call_args.kwargs["limits"].timeout, 10)
        web.return_value.fetch_url.assert_called_once_with(url)
        self.assertIn("[UNTRUSTED WEB CONTENT]", result["hookSpecificOutput"]["additionalContext"])

    def test_webfetch_distinguishes_http_failure_from_page_text(self):
        self.assertEqual(webfetch_status("The server returned HTTP 403 Forbidden."), 403)
        with patch("evo.adapters.harness.native_hooks.WebTools") as web:
            for text in ("The server returned HTTP 403 Forbidden.", "A page about requests that timed out"):
                run_hook({"hook_event_name": "PostToolUse", "tool_name": "WebFetch",
                          "tool_input": {"url": "https://public.example.net/"}, "tool_response": {"result": text}})
        web.assert_not_called()
