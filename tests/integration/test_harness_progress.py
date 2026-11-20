import json
from dataclasses import replace

from evo.adapters.harness.provider import ProviderHarnessAdapter
from evo.config import HarnessConfig
from tests.fixtures.harness import HarnessFixture
from tests.integration.test_harness_realistic import NativeModel, action


class ProgressHarnessTests(HarnessFixture):
    def native(self, model):
        return ProviderHarnessAdapter(self.state, self.broker, provider=model,
                                      config=HarnessConfig(adapter="provider"))

    def record(self, adapter, handle):
        return json.loads((adapter.root / (handle.run_id + ".json")).read_text())

    def test_repeated_listing_recovers_to_verified_delivery_without_spending_tool_budget(self):
        owner = self
        class Recovering(NativeModel):
            def decide(self, messages, tools):
                self.messages = messages
                self.tools = tools
                return super().decide(messages, tools)
        def advance():
            context = model.contexts[-1]
            owner.assertIn("Repeated unchanged action suppressed", context["feedback"])
            owner.assertEqual(context["progress"]["required_delivery"]["path"], "report.md")
            owner.assertFalse(context["progress"]["delivery_recorded"])
            owner.assertNotIn("list_files", [t["name"] for t in model.tools])
            owner.assertIn("tool", [m["role"] for m in model.messages])
            return action("write_file", path="report.md", content="delivered")
        model = Recovering([action("list_files"), action("list_files", path="."), action("list_files"),
                            advance, {"type": "done", "text": "Delivered with evidence"}])
        adapter = self.native(model)
        request = replace(self.request, acceptance={"path": "report.md", "exact": "delivered"})
        handle, result = self.complete(request, adapter)
        self.assertEqual(result.status, "success")
        record = self.record(adapter, handle)
        self.assertEqual(record["tool_calls"], 3)
        self.assertEqual(record["model_turns"], 5)
        self.assertEqual(len([e for e in record["events"] if e["kind"] == "repetition_suppressed"]), 1)

    def test_distinct_file_ranges_remain_available_for_legitimate_inspection(self):
        self.fs.write_file("source.py", "# source\n" * 300, purpose="code")
        model = NativeModel([action("read_file", path="source.py", purpose="code", start_line=i, max_lines=10)
                             for i in range(1, 201, 10)] +
                            [action("write_file", path="report.md", content="reviewed"),
                             {"type": "done", "text": "Reviewed the requested ranges"}])
        adapter = self.native(model)
        handle, result = self.complete(adapter=adapter)
        self.assertEqual(result.status, "success")
        self.assertEqual(self.record(adapter, handle)["tool_calls"], 21)
        self.assertFalse(any(e.kind == "repetition_suppressed" for e in adapter.events(handle)))

    def test_external_edit_during_model_call_invalidates_observation_counts(self):
        self.fs.write_file("source.py", "VALUE=1\n", purpose="code")
        def after_external_edit():
            self.fs.write_file("source.py", "VALUE=2\n", purpose="code")
            return action("read_file", path="source.py", purpose="code")
        model = NativeModel([action("read_file", path="source.py", purpose="code"),
                             action("read_file", path="source.py", purpose="code"), after_external_edit,
                             {"type": "done", "text": "Read changed source"}])
        adapter = self.native(model)
        handle, result = self.complete(adapter=adapter)
        self.assertEqual(result.status, "success")
        self.assertEqual(model.contexts[-1]["completed_platform_calls"][-1]["outcome"]["result"], "VALUE=2\n")
        self.assertFalse(any(e.kind == "repetition_suppressed" for e in adapter.events(handle)))

    def test_uncooperative_repetition_stops_as_blocked_without_claiming_success(self):
        adapter = self.native(NativeModel([action("list_files")] * 20))
        handle, result = self.complete(adapter=adapter)
        self.assertEqual(result.status, "blocked")
        self.assertIn("No progress", result.text)
        record = self.record(adapter, handle)
        self.assertEqual(record["tool_calls"], 2)
        self.assertEqual(record["model_turns"], 8)
        self.assertEqual(record["artifacts"], [])

    def test_repetition_counts_survive_approval_resume(self):
        self.fs.write_file("verify.py", "print('ok')\n", purpose="code")
        model = NativeModel([action("list_files"), action("list_files"), action("run_python", path="verify.py"),
                             action("list_files"), action("write_file", path="report.md", content="verified"),
                             {"type": "done", "text": "Verified"}])
        first_adapter = self.native(model)
        _, first = self.complete(adapter=first_adapter)
        self.assertEqual(first.status, "waiting_human")
        self.approval.answer(self.store.list_pending_requests()[0]["id"], {"approved": True})
        restored = self.native(model)
        handle = restored.resume(first.resume_ref, self.request)
        result = restored.wait(handle, 5)
        self.assertEqual(result.status, "success")
        self.python.run_python.assert_called_once()
        record = self.record(restored, handle)
        self.assertEqual(record["tool_calls"], 4)
        self.assertEqual(len([e for e in record["events"] if e["kind"] == "repetition_suppressed"]), 1)

    def test_identical_append_chunks_are_not_repetitions_when_file_changes(self):
        model = NativeModel([action("write_file", path="report.md", content="x")] +
                            [action("write_file", path="report.md", content="x", append=True)] * 5 +
                            [{"type": "done", "text": "Delivered six chunks"}])
        adapter = self.native(model)
        handle, result = self.complete(adapter=adapter)
        self.assertEqual(result.status, "success")
        self.assertEqual(self.fs.read_file("report.md"), "xxxxxx")
        self.assertEqual(self.record(adapter, handle)["tool_calls"], 6)
        self.assertFalse(any(e.kind == "repetition_suppressed" for e in adapter.events(handle)))

    def test_legacy_json_protocol_also_recovers_after_suppressed_repetition(self):
        class Legacy:
            def __init__(self):
                self.turns = 0
            def complete(self, messages, json_object=False):
                self.turns += 1
                context = json.loads(messages[-1]["content"])
                if self.turns <= 3:
                    return action("list_files")
                if self.turns == 4:
                    self.feedback = context["feedback"]
                    return action("write_file", path="report.md", content="delivered")
                return {"type": "done", "text": "Delivered"}
        model = Legacy()
        adapter = self.native(model)
        _, result = self.complete(adapter=adapter)
        self.assertEqual(result.status, "success")
        self.assertIn("Repeated unchanged action suppressed", model.feedback)
