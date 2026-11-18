import json
import tempfile
import unittest
from pathlib import Path

from evo.evaluation.dataset import load_dataset
from evo.evaluation.report import verify_report, write_report
from evo.evaluation.runner import EvaluationRunner


class Workspace:
    def __init__(self, root):
        self.workspace = root / "builder"
        self.workspace.mkdir(exist_ok=True)

    def read_file(self, path, *, purpose="document"):
        return (self.workspace / path).read_text()


class App:
    def __init__(self, root, *, preseed=False, write=False, status="success"):
        self.root = root
        self.workspace = Workspace(root)
        self.write = write
        self.status = status
        if preseed:
            (self.workspace.workspace / "report.md").write_text("done")

    def workspace_factory(self, agent):
        assert agent == "builder"
        return self.workspace

    def run(self, prompt, *, checks, memory_enabled):
        if self.write:
            (self.workspace.workspace / "report.md").write_text("done")
        return {"task": {"status": self.status, "result": "done"},
                "subtasks": [{"status": self.status, "result": "done"}]}


class EvaluationChecksTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "cases.jsonl"
        self.data.write_text('{"id":"c1","prompt":"Report","expected":[{"kind":"file_exact","agent":"builder","path":"report.md","text":"done"}]}\n')

    def test_stale_file_and_failed_run_cannot_pass(self):
        cases = load_dataset(self.data)
        stale = EvaluationRunner(lambda root: App(root, preseed=True)).run(cases)
        self.assertFalse(stale["cases"][0]["passed"])
        self.assertEqual(stale["objective_completion_rate"], 0)
        failed = EvaluationRunner(lambda root: App(root, write=True, status="failed")).run(cases)
        self.assertFalse(failed["cases"][0]["passed"])
        self.assertFalse(failed["cases"][0]["checks"][0]["passed"])

    def test_report_is_recomputable_and_contains_no_prompt(self):
        cases = load_dataset(self.data)
        report = EvaluationRunner(lambda root: App(root, write=True)).run(cases, memory_enabled=False)
        self.assertEqual(report["objective_completion_rate"], 1)
        self.assertTrue(verify_report(report, cases))
        output = self.root / "result.json"
        write_report(report, output, cases)
        saved = json.loads(output.read_text())
        self.assertTrue(verify_report(saved, cases))
        self.assertNotIn("Report", output.read_text())
        saved["objective_completion_rate"] = 0
        self.assertFalse(verify_report(saved, cases))

    def test_unregistered_test_check_rejected_before_execution(self):
        self.data.write_text('{"id":"c1","prompt":"Report","expected":[{"kind":"test","name":"trusted_check"}]}\n')
        cases = load_dataset(self.data)
        called = []
        with self.assertRaisesRegex(ValueError, "Unknown trusted"):
            EvaluationRunner(lambda root: called.append(root)).run(cases)
        self.assertEqual(called, [])

    def test_result_file_exists_and_trusted_test_checks(self):
        self.data.write_text(
            '{"id":"c1","prompt":"Report","expected":['
            '{"kind":"result_contains","text":"done"},'
            '{"kind":"file_exists","agent":"builder","path":"report.md"},'
            '{"kind":"test","name":"check_state"}]}\n')
        cases = load_dataset(self.data)
        seen = []
        def check_state(case, snapshot, state):
            seen.append(state)
            return (state / "builder" / "report.md").read_text() == "done"
        report = EvaluationRunner(lambda root: App(root, write=True),
                                  trusted_tests={"check_state": check_state}).run(cases)
        self.assertTrue(report["cases"][0]["passed"])
        self.assertEqual([item["passed"] for item in report["cases"][0]["checks"]],
                         [True, True, True])
        self.assertFalse(seen[0].exists())

    def test_self_reported_case_has_no_objective_denominator(self):
        self.data.write_text('{"id":"c1","prompt":"Report"}\n')
        cases = load_dataset(self.data)
        report = EvaluationRunner(lambda root: App(root)).run(cases)
        self.assertEqual(report["cases"][0]["basis"], "self_reported")
        self.assertEqual(report["objective_count"], 0)
        self.assertIsNone(report["objective_completion_rate"])
        self.assertEqual(report["goal_completion_rate"], 1)
        self.assertTrue(verify_report(report, cases))

    def test_provider_failure_is_redacted_from_machine_report(self):
        cases = load_dataset(self.data)
        class FailingApp(App):
            def run(self, prompt, *, checks, memory_enabled):
                raise RuntimeError("API_KEY=private-test-secret")
        report = EvaluationRunner(lambda root: FailingApp(root)).run(cases)
        self.assertEqual(report["cases"][0]["status"], "error")
        self.assertFalse(report["cases"][0]["passed"])
        self.assertTrue(verify_report(report, cases))
        self.assertNotIn("private-test-secret", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
