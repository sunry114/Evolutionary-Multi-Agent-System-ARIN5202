"""Bounded DAG planning and clarification from the original Engine planner."""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional, Tuple

from evo.domain.models import Agent, Subtask
from evo.domain.policies import MAX_SUBTASK_DESCRIPTION, PlanValidationError, validate_plan


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
    def __init__(self, provider, agents, approval=None, *, discussion_loader=None, events=None):
        self.provider = provider
        self.agents = tuple(agents)
        self.approval = approval
        self.discussion_loader = discussion_loader
        self.events = events

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
        discussion = self.discussion_loader(task_id) if self.discussion_loader and task_id else []
        messages = [{"role": "system", "content":
              "Decompose the goal into 1 to 5 independently verifiable subtasks. "
              "Use IDs s1, s2, etc. Dependencies may refer only to earlier IDs. "
              "Default to one owner and one subtask for writing, summarizing, translating, "
              "simple research, coding or data conversion. Split only when distinct "
              "deliverables really need different skills; do not create a subtask per role. "
              "The last subtask must assemble the final requested deliverable and depend "
              "on every earlier result it needs. Skills should match agent specialties. "
              "Use an exact agent name in skills when that role should own the subtask. "
              "Runtime: files may include Python, JS/TS, HTML/CSS and SQL, but execution "
              "only supports Python standard-library scripts in isolated Docker. "
              "There is no Node/npm, shell or package installation tool. Prefer a Python "
              "stdlib backend with HTML/CSS/JS for a runnable web app unless the human "
              "explicitly requires another stack. If a required stack cannot be tested "
              "here, request clarification about that constraint before planning. "
              "For a simple artifact use one subtask. Make reasonable choices for optional "
              "formatting and presentation details. A critical input is a "
              "specific artifact, target or constraint needed to produce a "
              "verifiable result. If it is absent or ambiguous, return one "
              "clarification question instead of inventing it. Return JSON "
              '{"question":null,"subtasks":[{"id":"s1",'
              '"description":"Describe the requested deliverable concisely",'
              '"skills":["agent name or specialty"],"depends_on":[]}]}. '
              'Each description must be at most %d characters; keep all required constraints.'
              % MAX_SUBTASK_DESCRIPTION},
             {"role": "user", "content": json.dumps(
                 {"goal": goal, "agents": specs, "required_inputs": required_inputs,
                  "team_discussion": discussion},
                 ensure_ascii=False)}]
        for attempt in range(3):
            try:
                result = self.provider.complete(messages, json_object=True)
            except Exception as error:
                if getattr(error, "code", None) != "invalid_completion":
                    raise
                reason = "invalid_completion"
                failure = error
            else:
                try:
                    plan = self._parse_result(result)
                except ValueError as error:
                    reason = "invalid_plan"
                    failure = error
                else:
                    if plan.question:
                        return self._clarify(plan.question, task_id)
                    return plan
            if self.events is not None and task_id:
                details = failure.details if isinstance(failure, PlanValidationError) else {}
                self.events.append_event(
                    task_id=task_id, event_type="planning_attempt_failed", actor="planner",
                    payload={"attempt": attempt + 1, "code": reason, "retrying": attempt < 2, **details})
            if attempt == 2:
                raise failure
            feedback = ("Validation error: " + str(failure) + ". "
                        if isinstance(failure, PlanValidationError) else "")
            messages = messages[:2] + [{"role": "user", "content": feedback +
                "The previous response was invalid. Return only a valid JSON object: "
                '{"question":null,"subtasks":[{"id":"s1","description":"Deliver the result",'
                '"skills":[],"depends_on":[]}]}. Use double-quoted keys and strings, '
                "JSON null, and arrays for skills and depends_on. Use 1 to 5 sequential IDs, "
                "descriptions of at most %d characters, and only earlier dependencies. "
                "If a critical input is missing, return a nonempty question instead."
                % MAX_SUBTASK_DESCRIPTION}]

    @staticmethod
    def _parse_result(result):
        if not isinstance(result, dict):
            raise PlanValidationError("not_object", "Planner must return a JSON object", field="response")
        question = result.get("question")
        if question is not None:
            if not isinstance(question, str) or not question.strip():
                raise PlanValidationError("invalid_question", "question must be null or a nonblank string", field="question")
            return PlanResult(question=question)
        raw = result.get("subtasks")
        if not isinstance(raw, list):
            raise PlanValidationError("invalid_subtasks", "subtasks must be an array", field="subtasks")
        subtasks = []
        for index, item in enumerate(raw):
            field = "subtasks[%d]" % index
            if not isinstance(item, dict):
                raise PlanValidationError("invalid_subtask", field + " must be an object", field=field)
            skills, dependencies = item.get("skills", []), item.get("depends_on", [])
            if (not isinstance(skills, list) or not all(isinstance(s, str) for s in skills)
                    or not isinstance(dependencies, list)
                    or not all(isinstance(d, str) for d in dependencies)):
                raise PlanValidationError("invalid_lists", field + ".skills and .depends_on must be arrays of strings", field=field)
            identifier, description = item.get("id"), item.get("description")
            if not isinstance(identifier, str) or not isinstance(description, str):
                raise PlanValidationError("invalid_fields", field + ".id and .description must be strings", field=field)
            subtasks.append(Subtask(identifier, description, tuple(skills), tuple(dependencies)))
        validate_plan(subtasks)
        return PlanResult(tuple(subtasks))
