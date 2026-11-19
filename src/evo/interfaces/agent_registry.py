"""Validated, durable role configuration for the local workspace."""

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from fcntl import LOCK_EX, flock
from pathlib import Path

from evo.adapters.tools.filesystem import validate_agent
from evo.domain.models import Agent, Squad


@dataclass(frozen=True)
class WorkspaceRegistry:
    agents: tuple[Agent, ...]
    squad: Squad

    def payload(self):
        return {"agents": [asdict(agent) for agent in self.agents],
                "squad": {**asdict(self.squad), "members": list(self.squad.members)}}


def parse_agents(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError("Provide 1 to 8 agent roles")
    agents = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "specialty"}:
            raise ValueError("Each agent needs a name and specialty")
        name, specialty = item["name"], item["specialty"]
        validate_agent(name)
        if name in {"human", "collective", "planner", "orchestrator"}:
            raise ValueError("Agent name is reserved")
        if (not isinstance(specialty, str) or not specialty.strip()
                or len(specialty.encode("utf-8")) > 1000):
            raise ValueError("Invalid agent specialty")
        agents.append(Agent(name, specialty.strip()))
    if len({agent.name for agent in agents}) != len(agents):
        raise ValueError("Agent names must be unique")
    return tuple(agents)


def default_registry(agents):
    agents = tuple(agents)
    names = tuple(agent.name for agent in agents)
    if not names or len(set(names)) != len(names):
        raise ValueError("Agent registry must be nonempty and unique")
    return WorkspaceRegistry(agents, Squad("default", names[0], names))


def parse_registry(value):
    if not isinstance(value, dict) or set(value) != {"agents", "squad"}:
        raise ValueError("Provide agents and one Squad")
    agents = parse_agents(value["agents"])
    squad = value["squad"]
    if not isinstance(squad, dict) or set(squad) != {"name", "lead", "members"}:
        raise ValueError("Squad requires a name, lead and members")
    validate_agent(squad["name"])
    members = squad["members"]
    names = {agent.name for agent in agents}
    if (not isinstance(members, list) or not members
            or any(not isinstance(member, str) or member not in names for member in members)
            or len(members) != len(set(members)) or squad["lead"] not in members):
        raise ValueError("Squad members must be unique registered agents and include the lead")
    return WorkspaceRegistry(agents, Squad(squad["name"], squad["lead"], tuple(members)))


def load_registry(state, default):
    path = Path(state) / "agents.json"
    return parse_registry(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else default


@contextmanager
def _registry_write(state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    with (state / ".agents.lock").open("a") as lock:
        flock(lock, LOCK_EX)
        yield state / "agents.json"


def migrate_registry(state):
    """Upgrade the old on-disk role array before the strict registry reader runs."""
    if not (Path(state) / "agents.json").exists():
        return
    with _registry_write(state) as path:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, list):
            _write_registry(path, default_registry(parse_agents(value)))


def save_registry(state, registry):
    with _registry_write(state) as path:
        _write_registry(path, registry)


def _write_registry(path, registry):
    data = json.dumps(registry.payload(), ensure_ascii=False, indent=2)
    descriptor, temporary = tempfile.mkstemp(prefix=".agents-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
