"""Auditable all-agent bidding, adapted from the original Engine assignment."""

import math
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


class AssignmentService:
    def __init__(self, provider, agents, events=None, memory_adjustment=None):
        self.provider = provider
        self.agents = tuple(agents)
        if not self.agents or len({agent.name for agent in self.agents}) != len(self.agents):
            raise ValueError("Agents must have distinct names")
        self.events = events
        self.memory_adjustment = memory_adjustment

    def assign(self, subtask: Subtask, task_id: Optional[str] = None) -> Assignment:
        bids = []
        for agent in self.agents:
            result = self.provider.complete(
                [{"role": "system", "content":
                  "You are %s, specializing in %s. Bid honestly from 0 to 5. "
                  "Return JSON {score,reason,proposed_approach,risks}."
                  % (agent.name, agent.specialty)},
                 {"role": "user", "content": subtask.description}],
                json_object=True)
            if not isinstance(result, dict):
                raise ValueError("Invalid bid")
            score = result.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 5:
                raise ValueError("Bid score must be between 0 and 5")
            reason = result.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("Bid reason is required")
            adjustment = 0.0
            if self.memory_adjustment is not None:
                adjustment = float(self.memory_adjustment(agent.name, subtask.description))
                if not math.isfinite(adjustment):
                    raise ValueError("Invalid memory adjustment")
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
                                         payload=bid.__dict__)
        winner = sorted(bids, key=lambda b: (-b.final_score, -b.specialty_matches, b.agent))[0]
        if self.events is not None and task_id is not None:
            self.events.append_event(task_id=task_id, event_type="agent_assigned", actor=winner.agent,
                                     payload={"subtask_id": subtask.id, "owner": winner.agent})
        return Assignment(winner.agent, tuple(bids))
