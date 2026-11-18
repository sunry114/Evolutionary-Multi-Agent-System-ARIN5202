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
                    if code not in {"authentication", "permission", "endpoint", "rate_limit",
                                    "http_error", "tls", "connection", "invalid_response",
                                    "invalid_completion", "provider_error"}:
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
                if self.events is not None and task_id is not None:
                    self.events.append_event(task_id=task_id, event_type="agent_bid_retry",
                                             actor=agent.name,
                                             payload={"subtask_id": subtask.id, "attempt": attempt + 1})
            else:
                raise AssignmentError("invalid_bid", agent.name)
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
