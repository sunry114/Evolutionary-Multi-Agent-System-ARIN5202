"""Browser-only provider and deterministic harness, wired through real application operations."""

import json
import re
from dataclasses import replace
from time import monotonic

from evo.adapters.harness.baseline import BaselineHarnessAdapter
from evo.adapters.harness.output import output_preview
from evo.interfaces import execution_factory
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.ports.harness import ToolCall


class FixtureProvider:
    def complete(self, messages, json_object=False):
        system = messages[0]["content"]
        context = json.loads(messages[1]["content"])
        if "Decompose the goal" in system:
            return {"subtasks": [{"id": "s1", "description": context["goal"]}]}
        if "Act as Squad captain" in system:
            if context["round"] == 1:
                return {"decision": "nominate", "member": "builder", "reason": "熟悉这份报告", "scope": ""}
            return {"decision": "nominate", "member": "reviewer", "reason": "由复核员独立核对证据",
                    "scope": "附上独立证据"}
        if "Respond to the proposed assignment" in system:
            if context["proposal"]["to"] == "builder":
                return {"decision": "revise", "reason": "报告需要独立复核", "scope": "附上独立证据"}
            return {"decision": "accept", "reason": "可以独立复核并交付报告"}
        agent = re.search(r"You are ([^,]+),", system).group(1)
        human_messages = [row["content"] for row in context.get("history", []) if row["sender"] == "human"]
        human_messages.append(context["latest_message"]["content"])
        return {"reply": agent + " 回复 " + " / ".join(human_messages)}


class FixtureHarness(BaselineHarnessAdapter):
    def _execute(self, control, request):
        if self.broker.issue_comment:
            with self.broker.repository._connect() as connection:
                run = connection.execute("SELECT kind,retry_of FROM issue_runs WHERE id=?", (request.task_id,)).fetchone()
            self._event(control, "assistant", {"text": "正在处理 " + request.agent})
            if "模拟模型连接中断" in request.prompt:
                self._event(control, "api_retry", {"text": "Claude Code API retry: connection_error"})
                terminal = "API Error: 模型连接中断\n\n" + "x" * 9000 + "\n\nClaude Code 错误末尾"
                self._event(control, "terminal_output", {"text": terminal})
                return "failed", terminal[:8192], "cli_failed", 1
            if "慢速工单" in request.prompt and not run["retry_of"]:
                progress_signal = self.root.parent / "browser-progress"
                deadline = monotonic() + 20
                while monotonic() < deadline and not progress_signal.is_file():
                    if control.stop.wait(0.1):
                        return "cancelled", "", "", None
                self._event(control, "assistant", {"text": "正在核对交付结果"})
                if control.stop.wait(max(0, deadline - monotonic())):
                    return "cancelled", "", "", None
            if "慢速工单" in request.prompt and run["retry_of"]:
                preview, truncated = output_preview("工具诊断样本\n" + "x" * 9000)
                self._event(control, "native_tool", {"name": "Bash", "status": "completed",
                                                       "output": preview, "output_truncated": truncated})
            if self.broker.issue_assign_work and run["kind"] == "assignment":
                self._call(control, request, "assign_issue_work", {"agent": "reviewer", "content": "独立核对证据"})
                return "success", "已发给复核员", "", 0
            if request.agent == "reviewer" and run["kind"] == "mention":
                return "success", "已独立核对证据", "", 0
            self._call(control, request, "write_file", {"path": "report.md", "content": "verified local report"})
            if self.broker.issue_status:
                self._call(control, request, "set_issue_status", {"status": "in_review"})
            if "慢速工单" in request.prompt and run["retry_of"]:
                return "success", ("**已完成本轮执行**\n\n" + "报告正文。\n\n" * 20 +
                                   "[查看报告末尾](https://example.com/report)"), "", 0
            return "success", "**已完成本轮执行**", "", 0
        revised = "Review feedback:" in request.prompt
        prepare = []
        if not revised:
            scratch = "approval-" + request.task_id + ".md"
            prepare = [ToolCall("write_file", {"path": scratch, "content": "temporary evidence"}),
                       ToolCall("delete_file", {"path": scratch})]
        content = "revised review deliverable" if revised else "original review deliverable"
        deliver = ToolCall("write_file", {"path": "report.md", "content": content, "overwrite": True})
        return super()._execute(control, replace(request, actions=tuple(prepare + [deliver])))


def create_application(*, state):
    def fixture_harness(config, state_path, broker, **_kwargs):
        return FixtureHarness(state_path, broker, config=config)

    execution_factory.create_harness = fixture_harness
    return SQLiteApplication(state, provider=FixtureProvider())
