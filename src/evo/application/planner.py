"""Bounded DAG planning and clarification from the original Engine planner."""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional, Tuple

from evo.domain.models import Agent, Subtask
from evo.domain.policies import validate_plan


@dataclass(frozen=True)
class PlanResult:
    subtasks: Tuple[Subtask, ...] = ()
    question: Optional[str] = None
    request_id: Optional[str] = None


class StoredClarificationRequests:
    """Save planner questions through the existing approval repository."""

    def __init__(self, repository):
        self.repository = repository

    def request_clarification(self, task_id: str, question: str) -> str:
        request_id = str(uuid.uuid4())
        self.repository.save_approval_request(
            request_id=request_id, task_id=task_id, agent="planner",
            kind="clarification", payload={"question": question})
        return request_id


class Planner:
    def __init__(self, provider, agents, approval=None):
        self.provider = provider
        self.agents = tuple(agents)
        self.approval = approval

    def _clarify(self, question, task_id):
        if self.approval is None or not task_id:
            raise ValueError("Clarification requires a request store and task_id")
        question = question.strip()[:500]
        request_id = self.approval.request_clarification(task_id, question)
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("Clarification store did not return a request ID")
        return PlanResult(question=question, request_id=request_id)

    def plan(self, goal, task_id=None, required_inputs=None):
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("Goal cannot be empty")
        # Callers can declare inputs that must exist before planning. None,
        # blank text and empty containers mean missing; false and zero are valid.
        if required_inputs is None:
            required_inputs = {}
        if not isinstance(required_inputs, Mapping) or not all(
                isinstance(name, str) and name.strip() for name in required_inputs):
            raise ValueError("Required inputs must be a mapping with named keys")
        missing = [name for name, value in required_inputs.items()
                   if value is None or (isinstance(value, str) and not value.strip())
                   or (isinstance(value, (list, tuple, dict, set)) and not value)]
        if missing:
            return self._clarify("Please provide the missing required input(s): "
                                 + ", ".join(missing) + ".", task_id)
        specs = {agent.name: agent.specialty for agent in self.agents}
        result = self.provider.complete(
            [{"role": "system", "content":
              "Decompose the goal into 1 to 5 independently verifiable subtasks. "
              "Use IDs s1, s2, etc. Dependencies may refer only to earlier IDs. "
              "For a simple artifact use one subtask. A critical input is a "
              "specific artifact, target or constraint needed to produce a "
              "verifiable result. If it is absent or ambiguous, return one "
              "clarification question instead of inventing it. Return JSON "
              "{question:null,subtasks:[{id,description,skills,depends_on}]}"},
             {"role": "user", "content": json.dumps(
                 {"goal": goal, "agents": specs, "required_inputs": required_inputs},
                 ensure_ascii=False)}],
            json_object=True)
        if not isinstance(result, dict):
            raise ValueError("Planner must return a JSON object")
        question = result.get("question")
        if question is not None:
            if not isinstance(question, str) or not question.strip():
                raise ValueError("Invalid clarification question")
            return self._clarify(question, task_id)
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
