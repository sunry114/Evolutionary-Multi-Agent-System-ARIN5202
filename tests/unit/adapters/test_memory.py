import tempfile
import unittest

from evo.adapters.persistence.sqlite_memory import SQLiteMemoryStore
from evo.domain.models import Experience
from evo.domain.statuses import TaskStatus


class MemoryTests(unittest.TestCase):
    def test_recall_is_relevant_deduplicated_bounded_and_outcome_aware(self):
        with tempfile.TemporaryDirectory() as root:
            memory = SQLiteMemoryStore(root, "builder")
            for task, outcome, lesson in (
                ("Python report generation", TaskStatus.SUCCESS, "Check the generated report"),
                ("Python report generation", TaskStatus.SUCCESS, "Check the generated report"),
                ("Python report generation", TaskStatus.FAILED, "Do not trust an empty file"),
                ("Python report generation", TaskStatus.BLOCKED, "Approval was refused"),
                ("Python report generation", TaskStatus.SUCCESS, "Validate the document path"),
                ("Weather forecast", TaskStatus.SUCCESS, "Look outside"),
            ):
                memory.add(Experience("builder", task, outcome, lesson))
            found = memory.recall("Python report generation", 3)
            self.assertEqual(len(found), 3)
            self.assertEqual(len({item.lesson for item in found}), 3)
            self.assertNotIn("Look outside", [item.lesson for item in found])
            self.assertTrue(all(item.agent == "builder" for item in found))
            self.assertEqual(memory.adjustment("Unrelated astronomy"), 0)
            self.assertLessEqual(abs(memory.adjustment("Python report generation")), 0.75)

    def test_cross_agent_write_and_non_terminal_outcome_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            memory = SQLiteMemoryStore(root, "builder")
            for item in (Experience("reviewer", "Python report", TaskStatus.SUCCESS, "lesson"),
                         Experience("builder", "Python report", TaskStatus.RUNNING, "lesson")):
                with self.assertRaises(ValueError):
                    memory.add(item)
            self.assertEqual(memory.recall("Python report"), ())


if __name__ == "__main__":
    unittest.main()
