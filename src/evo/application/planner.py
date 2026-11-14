"""Bounded DAG planning and clarification from the original Engine planner."""

import json
from dataclasses import dataclass
from typing import Optional, Tuple

from evo.domain.models import Agent, Subtask
from evo.domain.policies import validate_plan


@dataclass(frozen=True)
class PlanResult:
    subtasks: Tuple[Subtask, ...] = ()
    question: Optional[str] = None
    request_id: Optional[str] = None


class Planner:
    def __init__(self, provider, agents, approval=None):
        self.provider = provider
        self.agents = tuple(agents)
        self.approval = approval

    def plan(self, goal, task_id=None):
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("Goal cannot be empty")
        specs = {agent.name: agent.specialty for agent in self.agents}
        result = self.provider.complete(
            [{"role": "system", "content":
              "Decompose the goal into 1 to 5 independently verifiable subtasks. "
              "Use IDs s1, s2, etc. Dependencies may refer only to earlier IDs. "
              "For a simple artifact use one subtask. If critical input is missing, "
              "return a clarification question instead of guessing. Return JSON "
              "{question:null,subtasks:[{id,description,skills,depends_on}]}"},
             {"role": "user", "content": json.dumps({"goal": goal, "agents": specs}, ensure_ascii=False)}],
            json_object=True)
        if not isinstance(result, dict):
            raise ValueError("Planner must return a JSON object")
        question = result.get("question")
        if question is not None:
            if not isinstance(question, str) or not question.strip():
                raise ValueError("Invalid clarification question")
            question = question.strip()[:500]
            request_id = None
            if self.approval is not None and task_id is not None:
                request_id = self.approval.request_clarification(task_id, question)
            return PlanResult(question=question, request_id=request_id)
        raw = result.get("subtasks")
        if not isinstance(raw, list):
            raise ValueError("Planner returned invalid subtasks")
        subtasks = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("Planner returned invalid subtask")
            skills, dependencies = item.get("skills", []), item.get("depends_on", [])
            if (not isinstance(skills, list) or not all(isinstance(s, str) for s in skills)
                    or not isinstance(dependencies, list)
                    or not all(isinstance(d, str) for d in dependencies)):
                raise ValueError("Invalid skills or dependencies")
            identifier, description = item.get("id"), item.get("description")
            if not isinstance(identifier, str) or not isinstance(description, str):
                raise ValueError("Invalid subtask ID or description")
            subtasks.append(Subtask(identifier, description, tuple(skills), tuple(dependencies)))
        validate_plan(subtasks)
        return PlanResult(tuple(subtasks))
