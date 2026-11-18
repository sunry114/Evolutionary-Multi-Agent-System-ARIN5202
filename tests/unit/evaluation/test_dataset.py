import tempfile
import unittest
from pathlib import Path

from evo.evaluation.dataset import load_dataset


def dataset(text):
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "cases.jsonl"
        path.write_text(text)
        return load_dataset(path)


class DatasetTests(unittest.TestCase):
    def test_objective_and_self_reported_cases(self):
        cases = dataset(
            '{"id":"one","prompt":"Make a report","expected":[{"kind":"file_contains","agent":"builder","path":"report.md","text":"done"}],"subtask_checks":{"s1":{"path":"report.md","exact":"done"}}}\n'
            '{"id":"two","prompt":"Describe result"}\n')
        self.assertEqual([case.basis for case in cases.cases], ["objective", "self_reported"])
        self.assertEqual(cases.cases[0].subtask_checks["s1"].exact, "done")
        self.assertEqual(len(cases.sha256), 64)

    def test_empty_duplicate_and_bad_schema_rejected(self):
        samples = ["", "\n", '{"id":"x","prompt":"ok"}\n{"id":"x","prompt":"again"}\n',
                   '{"id":"x","prompt":""}\n',
                   '{"id":"x","prompt":"ok","expected":[]}\n',
                   '{"id":"x","prompt":"ok","expected":[{"kind":"file_exists","agent":"builder","path":"../old.md"}]}\n',
                   '{"id":"x","prompt":"ok","expected":[{"kind":"file_contains","agent":"builder","path":"ok.md"}]}\n',
                   '{"id":"x","prompt":"ok","subtask_checks":{"s1":{"path":"/tmp/out.md","exact":"ok"}}}\n']
        for text in samples:
            with self.subTest(text=text), self.assertRaises(ValueError):
                dataset(text)
