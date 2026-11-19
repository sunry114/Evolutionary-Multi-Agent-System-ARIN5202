"""Auditable all-agent bidding, adapted from the original Engine assignment."""

import math
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from evo.domain.models import Subtask


@dataclass(frozen=True)
class Bid:
    agent: str
    raw_score: float
    adjustment: float
    final_score: float
    reason: str
    proposed_approach: str
    risks: str
    specialty_matches: int


@dataclass(frozen=True)
class Assignment:
    owner: str
    bids: Tuple[Bid, ...]


class AssignmentError(ValueError):
    """A safe failure category for task traces, without provider response text."""

    def __init__(self, code, agent):
        super().__init__("Agent assignment failed")
        self.code = code
        self.agent = agent


class AssignmentService:
    def __init__(self, provider, agents, events=None, memory_adjustment=None):
        self.provider = provider
        self.agents = tuple(agents)
        if not self.agents or len({agent.name for agent in self.agents}) != len(self.agents):
            raise ValueError("Agents must have distinct names")
        self.events = events
        self.memory_adjustment = memory_adjustment

    def assign(self, subtask: Subtask, task_id: Optional[str] = None,
               *, memory_enabled: bool = True) -> Assignment:
        bids = []
        for agent in self.agents:
            invalid_reason = "invalid_bid"
            for attempt in range(2):
                try:
                    result = self.provider.complete(
                        [{"role": "system", "content":
                          "You are %s, specializing in %s. Bid honestly from 0 to 5. "
                          "Return only JSON with numeric score between 0 and 5 and nonempty reason. "
                          "Keys: score, reason, proposed_approach, risks."
                          % (agent.name, agent.specialty)},
                         {"role": "user", "content": subtask.description +
                          ("\nYour previous bid was invalid. Return all required fields as JSON."
                           if attempt else "")}], json_object=True)
                except Exception as exc:
                    code = getattr(exc, "code", "provider_failure")
                    if code == "invalid_completion":
                        invalid_reason = code
                        # A completed request can still contain an empty, cut-off,
                        # or malformed bid. Treat it like an invalid bid: retry
                        # once, then score this agent neutrally so other bids
                        # can still determine the assignment.
                        if self.events is not None and task_id is not None:
                            self.events.append_event(task_id=task_id, event_type="agent_bid_retry",
                                                     actor=agent.name,
                                                     payload={"subtask_id": subtask.id,
                                                              "attempt": attempt + 1,
                                                              "reason": "invalid_completion"})
                        continue
                    if code not in {"authentication", "permission", "endpoint", "rate_limit",
                                    "http_error", "tls", "connection", "invalid_response",
                                    "provider_error"}:
                        code = "provider_failure"
                    raise AssignmentError(code, agent.name) from None
                score = result.get("score") if isinstance(result, dict) else None
                if isinstance(score, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", score.strip()):
                    score = float(score.strip())
                reason = result.get("reason") if isinstance(result, dict) else None
                valid = (not isinstance(score, bool) and isinstance(score, (int, float))
                         and math.isfinite(score) and 0 <= score <= 5
                         and isinstance(reason, str) and bool(reason.strip())
                         and isinstance(result.get("proposed_approach", ""), str)
                         and isinstance(result.get("risks", ""), str))
                if valid:
                    break
                invalid_reason = "invalid_bid"
                if self.events is not None and task_id is not None:
                    self.events.append_event(task_id=task_id, event_type="agent_bid_retry",
                                             actor=agent.name,
                                             payload={"subtask_id": subtask.id, "attempt": attempt + 1})
            else:
                # A malformed model bid must not stop every dependent task.
                # Keep the agent in the comparison with a neutral, auditable bid.
                result = {"score": 0, "reason": "Model bid invalid; neutral fallback"}
                score, reason = 0, result["reason"]
                if self.events is not None and task_id is not None:
                    self.events.append_event(task_id=task_id, event_type="agent_bid_fallback",
                                             actor=agent.name,
                                             payload={"subtask_id": subtask.id,
                                                      "reason": invalid_reason})
            adjustment = 0.0
            if memory_enabled and self.memory_adjustment is not None:
                try:
                    adjustment = float(self.memory_adjustment(agent.name, subtask.description))
                    if not math.isfinite(adjustment):
                        raise ValueError("Invalid memory adjustment")
                except Exception:
                    raise AssignmentError("memory_adjustment_failed", agent.name) from None
                adjustment = max(-1.0, min(1.0, adjustment))
            approach = result.get("proposed_approach", "")
            risks = result.get("risks", "")
            if not isinstance(approach, str) or not isinstance(risks, str):
                raise ValueError("Invalid bid explanation")
            matches = sum(skill.lower() in agent.specialty.lower() for skill in subtask.skills if skill)
            bid = Bid(agent.name, float(score), adjustment, float(score) + adjustment,
                      reason.strip()[:300], approach[:500], risks[:500], matches)
            bids.append(bid)
            if self.events is not None and task_id is not None:
                self.events.append_event(task_id=task_id, event_type="agent_bid", actor=agent.name,
                                         payload={"subtask_id": subtask.id, **bid.__dict__})
        winner = sorted(bids, key=lambda b: (-b.final_score, -b.specialty_matches, b.agent))[0]
        if self.events is not None and task_id is not None:
            self.events.append_event(task_id=task_id, event_type="agent_assigned", actor=winner.agent,
                                     payload={"subtask_id": subtask.id, "owner": winner.agent})
        return Assignment(winner.agent, tuple(bids))


class RoutingService:
    """Choose one owner locally; bids remain available for research experiments."""

    def __init__(self, agents, events=None):
        self.agents = tuple(agents)
        if not self.agents or len({a.name for a in self.agents}) != len(self.agents):
            raise ValueError("Agents must have distinct names")
        self.events = events
        self.memory_adjustment = None

    def assign(self, subtask, task_id=None, *, memory_enabled=True):
        # Skills supplied by the planner are stronger signals than incidental
        # words in the description. Prefer a general builder on otherwise equal
        # scores, then preserve registry order.
        tokens = set(re.findall(r"[\w]+", subtask.description.casefold()))
        def score(agent):
            specialty = agent.specialty.casefold()
            matches = sum(skill.casefold() in specialty for skill in subtask.skills if skill)
            words = set(re.findall(r"[\w]+", specialty + " " + agent.name.casefold()))
            return matches * 10 + len(tokens & words)
        requested = {skill.casefold() for skill in subtask.skills}
        winner = max(self.agents, key=lambda agent: (agent.name.casefold() in requested, score(agent),
                     agent.name.casefold() in {"builder", "writer", "implementer"}))
        if self.events is not None and task_id is not None:
            self.events.append_event(task_id=task_id, event_type="agent_assigned", actor=winner.name,
                                     payload={"subtask_id": subtask.id, "owner": winner.name,
                                              "strategy": "routing", "score": score(winner)})
        return Assignment(winner.name, ())


class TaskAssignmentService:
    """Use the task's saved strategy when a paused DAG continues after restart."""

    def __init__(self, services, strategy_for_task):
        self.services = services
        self.strategy_for_task = strategy_for_task
        self.memory_adjustment = None

    def assign(self, subtask, task_id=None, *, memory_enabled=True):
        service = self.services[self.strategy_for_task(task_id)]
        service.memory_adjustment = self.memory_adjustment
        return service.assign(subtask, task_id, memory_enabled=memory_enabled)
