import unittest

from evo.domain.models import Review, Subtask, Task
from evo.domain.policies import (CRITICAL_TOOLS, validate_document_path,
                                 validate_execution_budget, validate_plan)
from evo.domain.statuses import (ReviewStatus, TaskStatus,
                                 validate_review_transition,
                                 validate_task_transition)


class DomainTests(unittest.TestCase):
    def test_valid_plan_and_backward_dependency(self):
        validate_plan((Subtask("s1", "Build artifact"),
                       Subtask("s2", "Verify artifact", depends_on=("s1",))))

    def test_rejects_invalid_plans(self):
        bad = [(),
               tuple(Subtask("s%d" % i, "Task") for i in range(1, 7)),
               (Subtask("s2", "Task"),),
               (Subtask("s1", " "),),
               (Subtask("s1", "Task", depends_on=("s1",)),),
               (Subtask("s1", "Task", depends_on=("s2",)),),
               (Subtask("s1", "Task"), Subtask("s2", "Task", depends_on=("s1", "s1")))]
        for plan in bad:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                validate_plan(plan)

    def test_limits_and_critical_tools(self):
        validate_execution_budget(12, 20, 2, 3)
        for values in [(13, 0, 0, 0), (0, 21, 0, 0), (0, 0, 3, 0), (0, 0, 0, 4)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_execution_budget(*values)
        self.assertEqual(CRITICAL_TOOLS, {"run_python", "delete_file", "git_push"})

    def test_document_policy(self):
        validate_document_path("report.md")
        validate_document_path("notes.TXT")
        with self.assertRaises(ValueError):
            validate_document_path("report.pdf")

    def test_task_and_review_status_are_independent(self):
        task = Task("t1", "Build", status=TaskStatus.SUCCESS)
        review = Review(task.id)
        self.assertEqual(review.status, ReviewStatus.PENDING)
        validate_task_transition(TaskStatus.RUNNING, TaskStatus.WAITING_HUMAN)
        validate_task_transition(TaskStatus.WAITING_HUMAN, TaskStatus.RUNNING)
        validate_review_transition(ReviewStatus.PENDING, ReviewStatus.CHANGES_REQUESTED)
        with self.assertRaises(ValueError):
            validate_task_transition(TaskStatus.SUCCESS, TaskStatus.RUNNING)
        with self.assertRaises(ValueError):
            validate_review_transition(ReviewStatus.ACCEPTED, ReviewStatus.REVISING)


if __name__ == "__main__":
    unittest.main()
