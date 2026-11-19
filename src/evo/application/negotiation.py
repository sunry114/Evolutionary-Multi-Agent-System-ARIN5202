"""Bounded nomination and acceptance for Squad and direct issues."""

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ConfirmedAssignment:
    owner: str
    description: str
    round_number: int
    reason: str


@dataclass(frozen=True)
class AssignmentClarification:
    actor: str
    question: str
    round_number: int


@dataclass(frozen=True)
class AssignmentBlock:
    actor: str
    reason: str
    code: str
    round_number: int


class NegotiationService:
    def __init__(self, provider, store, agents, *, coordinator, recall, direct=False, initial_member=None,
                 excluded=()):
        self.provider = provider
        self.store = store
        self.agents = {agent.name: agent.specialty for agent in agents}
        self.coordinator = coordinator
        self.direct = direct
        self.initial_member = initial_member
        self.recall = recall
        self.members = {name: role for name, role in self.agents.items() if name not in excluded}

    def _call(self, actor, phase, instructions, context):
        issue_id = context["issue_id"]
        description = context["subtask"]["description"]
        lessons, cross_task = self.recall(issue_id, actor, description)
        if lessons:
            context = {**context, "private_memory": lessons}
            instructions += (" Use private memory only to inform your decision. Keep the notes private "
                             "and follow the current task, dependencies, and acceptance criteria.")
        self.store.append_event(
            task_id=issue_id, event_type="routing_call", actor=actor,
            payload={"task_id": context["task_id"], "phase": phase, "memory_count": len(lessons),
                     "cross_task": cross_task})
        result = self.provider.complete(
            [{"role": "system", "content": (
                f"You are {actor}, specializing in {self.agents[actor]}. " + instructions)},
             {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
            json_object=True)
        if not isinstance(result, dict):
            raise ValueError("Negotiation requires a JSON response")
        return result

    @staticmethod
    def _text(response, field, limit):
        value = response.get(field)
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
            raise ValueError("Invalid negotiation response field: " + field)
        return value.strip()

    def _nomination_decision(self, context):
        role = "the delegating member" if self.initial_member else "Squad captain"
        return self._call(self.coordinator, "nomination",
            f"Act as {role} for the current subtask. Nominate one listed member. "
            "Consider prior replies, including scope counterproposals, recommendations, and blockers. "
            "Choose a scope from offered_scopes, or use an empty scope for the original assignment. "
            "Preserve the original obligations, dependencies, and acceptance checks. "
            "If a proposal needs a change to the user's goal or acceptance, ask the human to clarify. "
            'Return JSON like {"decision":"nominate","member":"member_name","scope":"","reason":"One short sentence."}. '
            "Use the selected offered scope verbatim. For clarity use decision clarify with question and reason. "
            "Use decision blocked with reason if no member can do the work. Keep reason under 200 characters.",
            context)

    def _member_decision(self, member, context):
        choices = ("Use accept, blocked, or clarify with a question field." if self.direct else
                   'Use accept, revise, recommend, or blocked. For revise, include a separate scope field, '
                   'as in {"decision":"revise","scope":"Revised division of work","reason":"Why it helps"}. '
                   'For recommend, include a member field naming the suggested member.')
        return self._call(member, "member_reply",
            "Respond to the proposed assignment as the nominated member. " + choices +
            ' Return JSON like {"decision":"accept","reason":"One short sentence."}. '
            "Keep reason under 200 characters. Accept only if you will perform the "
            "proposed work. A scope counterproposal may detail or adjust how you divide the work "
            "but the original obligations and acceptance checks still apply. State conflicts in your reason.",
            context)

    def assign(self, root_id, task_id, goal, subtask, check, *, max_rounds):
        history = []
        answers = []
        rounds_used = 0
        for event in self.store.list_events(root_id):
            kind, entry = event["event_type"], event["payload"]
            if entry.get("task_id") != task_id:
                continue
            if kind in ("assignment_proposed", "assignment_reply", "assignment_question"):
                history.append(entry)
                rounds_used = max(rounds_used, entry["round"])
            elif kind == "assignment_clarification_requested":
                request = self.store.get_approval_request(entry["request_id"])
                if request["status"] == "answered":
                    answers.append({"question": entry["question"], "answer": request["answer"]["answer"]})

        context = {"issue_id": root_id, "task_id": task_id, "goal": goal, "subtask": asdict(subtask),
                   "acceptance": asdict(check) if check is not None else None,
                   "members": self.members, "history": history, "human_answers": answers}

        def record(kind, actor, recipient, round_number, **details):
            entry = {"task_id": task_id, "subtask_id": subtask.id, "round": round_number,
                     "from": actor, "to": recipient, **details}
            self.store.append_event(task_id=root_id, event_type=kind, actor=actor, payload=entry)
            history.append(entry)
            return entry

        reason = history[-1]["reason"] if history else "No member confirmed the work"
        for round_number in range(rounds_used + 1, max_rounds + 1):
            context["round"] = round_number
            context["max_rounds"] = max_rounds
            scope = ""
            member = self.coordinator
            proposer = "human" if self.direct else self.coordinator
            reason = "The user directly assigned the issue to this member."
            if self.initial_member and round_number == 1:
                member = self.initial_member
                reason = "The executing member requested this delegated work."
            elif not self.direct:
                offered = [""] + [entry["scope"] for entry in history if entry.get("decision") == "revise"]
                response = self._nomination_decision({**context, "offered_scopes": offered})
                reason = self._text(response, "reason", 500)
                decision = response.get("decision")
                if decision == "blocked":
                    actor = "Delegating member" if self.initial_member else "Captain"
                    return AssignmentBlock(proposer, actor + " reported: " + reason, "assignment_blocked", round_number)
                if decision == "clarify":
                    question = self._text(response, "question", 500)
                    record("assignment_question", proposer, "human", round_number, reason=reason,
                           decision=decision, question=question)
                    if round_number < max_rounds:
                        return AssignmentClarification(proposer, question, round_number)
                    break
                member = self._text(response, "member", 64)
                scope = response.get("scope")
                if decision != "nominate" or member not in self.members or scope not in offered:
                    raise ValueError("Invalid nomination")

            proposal = record("assignment_proposed", proposer, member, round_number, reason=reason, scope=scope)
            response = self._member_decision(member, {**context, "proposal": proposal})
            reason = self._text(response, "reason", 500)
            decision = response.get("decision")
            details = {}
            if decision == "revise" and not self.direct:
                details["scope"] = self._text(response, "scope", 1000)
                if details["scope"] == scope:
                    raise ValueError("A counterproposal must change the scope")
            elif decision == "recommend" and not self.direct:
                target = self._text(response, "member", 64)
                if target not in self.members or target == member:
                    raise ValueError("Invalid recommended member")
                details["member"] = target
            elif decision == "clarify" and self.direct:
                details["question"] = self._text(response, "question", 500)
            elif decision not in ("accept", "blocked"):
                raise ValueError("Invalid member decision")
            record("assignment_reply", member, proposer, round_number, reason=reason, decision=decision, **details)

            if decision == "accept":
                description = subtask.description
                if scope:
                    description += "\n\nAgreed scope (subject to the original requirements): " + scope
                return ConfirmedAssignment(member, description, round_number, reason)
            if self.direct and decision == "blocked":
                return AssignmentBlock(member, "Assigned member reported: " + reason, "assignment_blocked", round_number)
            if self.direct and decision == "clarify" and round_number < max_rounds:
                return AssignmentClarification(member, details["question"], round_number)

        return AssignmentBlock(self.coordinator, f"Negotiation exhausted after {max_rounds} rounds. "
                               f"Last reason: {reason}", "negotiation_exhausted", max_rounds)
