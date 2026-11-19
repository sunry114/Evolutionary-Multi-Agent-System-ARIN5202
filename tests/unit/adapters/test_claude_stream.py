"""System notices must not fill a Claude harness checkpoint."""

from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import patch

from evo.adapters.harness.claude_code import ClaudeCodeAdapter
from evo.config import HarnessConfig, HarnessModelConfig
from tests.fixtures.harness import FakeCLITransport, HarnessFixture


class ClaudeStreamTests(HarnessFixture):
    def setUp(self):
        super().setUp()
        config = HarnessConfig(adapter="claude_code", timeout=3)
        self.adapter = ClaudeCodeAdapter(
            self.state, self.broker, config=config,
            model=HarnessModelConfig(model="fixture-model"),
            credential_loader=lambda: "fixture-credential",
        )
        self.adapter.transport = FakeCLITransport(config)

    def _run_without_sockets(self, prompt, proxy_summary=None):
        proxy_summary = proxy_summary or {"requests": 0, "status_codes": {}, "failures": 0}

        class FakeProxy:
            requests = proxy_summary["requests"]

            def summary(self):
                return proxy_summary

        with patch("evo.adapters.harness.claude_code.MCPBridge", side_effect=lambda *a, **k: nullcontext()), \
                patch("evo.adapters.harness.claude_code.ModelProxy", side_effect=lambda *a, **k: nullcontext(FakeProxy())):
            return self.complete(replace(self.request, prompt=prompt))

    def test_repeated_system_notices_do_not_consume_event_budget(self):
        handle, result = self._run_without_sockets("system_noise")
        self.assertEqual(result.status, "success")
        events = self.adapter.events(handle)
        self.assertEqual(sum(event.kind == "started" for event in events), 1)
        self.assertEqual(len(events), 4)
        summary = next(event.payload for event in events if event.kind == "system_event_summary")
        self.assertEqual(summary, {"total": 41, "subtypes": {"init": 1, "thinking_tokens": 40}})
        finished = [event["payload"] for event in self.store.list_events("t1")
                    if event["event_type"] == "harness_finished"][-1]
        self.assertEqual(finished["system_event_summary"], summary)

    def test_model_status_summary_is_visible_in_task_trace(self):
        model_summary = {"requests": 2, "status_codes": {"429": 2}, "failures": 0}
        self._run_without_sockets("system_noise", model_summary)
        finished = [event["payload"] for event in self.store.list_events("t1")
                    if event["event_type"] == "harness_finished"][-1]
        self.assertEqual(finished["model_proxy_summary"], model_summary)

    def test_many_thinking_token_notices_do_not_stop_the_task(self):
        handle, result = self._run_without_sockets("many_thinking_tokens")
        self.assertEqual((result.status, result.error), ("success", ""))
        self.assertLess(len(self.adapter.events(handle)), 10)
        summary = next(event.payload for event in self.adapter.events(handle)
                       if event.kind == "system_event_summary")
        self.assertEqual(summary["total"], 1025)
        finished = [event["payload"] for event in self.store.list_events("t1")
                    if event["event_type"] == "harness_finished"][-1]
        self.assertEqual(finished["system_event_summary"], summary)
