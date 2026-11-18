"""Validated, durable role configuration for the local workspace."""

import json
import os
import tempfile
from pathlib import Path

from evo.adapters.tools.filesystem import validate_agent
from evo.domain.models import Agent


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


def load_agents(state, fallback):
    path = Path(state) / "agents.json"
    if not path.exists():
        return tuple(fallback)
    return parse_agents(json.loads(path.read_text(encoding="utf-8")))


def save_agents(state, agents):
    path = Path(state) / "agents.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps([{"name": agent.name, "specialty": agent.specialty}
                       for agent in agents], ensure_ascii=False, indent=2)
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
