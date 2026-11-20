"""Validated, durable role configuration for the local workspace."""

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from fcntl import LOCK_EX, flock
from pathlib import Path

from evo.adapters.tools.filesystem import PathPolicyError, validate_agent
from evo.domain.models import Agent, Squad


class RegistryValidationError(ValueError):
    """A configuration validation error safe to show to the user."""


@dataclass(frozen=True)
class WorkspaceRegistry:
    agents: tuple[Agent, ...]
    squad: Squad

    def payload(self):
        return {"agents": [asdict(agent) for agent in self.agents],
                "squad": {**asdict(self.squad), "members": list(self.squad.members)}}


def parse_agents(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise RegistryValidationError("请配置 1 至 8 个 Agent。")
    agents = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "specialty"}:
            raise RegistryValidationError("每个 Agent 都需要 name 和 specialty 字段。")
        name, specialty = item["name"], item["specialty"]
        try:
            validate_agent(name)
        except PathPolicyError:
            raise RegistryValidationError("Agent 名称须为 1 至 64 个英文字母、数字、下划线或连字符。") from None
        if name in {"human", "collective", "planner", "orchestrator"}:
            raise RegistryValidationError("该 Agent 名称为系统保留名称。")
        if (not isinstance(specialty, str) or not specialty.strip()
                or len(specialty.encode("utf-8")) > 1000):
            raise RegistryValidationError("Agent 专长不能为空，且长度不能超过 1000 字节。")
        agents.append(Agent(name, specialty.strip()))
    if len({agent.name for agent in agents}) != len(agents):
        raise RegistryValidationError("Agent 名称不能重复。")
    return tuple(agents)


def default_registry(agents):
    agents = tuple(agents)
    names = tuple(agent.name for agent in agents)
    if not names or len(set(names)) != len(names):
        raise ValueError("Agent registry must be nonempty and unique")
    return WorkspaceRegistry(agents, Squad("default", names[0], names))


def parse_registry(value):
    if not isinstance(value, dict) or set(value) != {"agents", "squad"}:
        raise RegistryValidationError("配置须包含 agents 和一个 squad。")
    agents = parse_agents(value["agents"])
    squad = value["squad"]
    if not isinstance(squad, dict) or set(squad) != {"name", "lead", "members"}:
        raise RegistryValidationError("小队须包含 name、lead 和 members 字段。")
    name = squad["name"].strip() if isinstance(squad["name"], str) else ""
    if not 1 <= len(name) <= 64 or any(not (char.isalnum() or char in " _-") for char in name):
        raise RegistryValidationError("小队名称须为 1 至 64 个字符，支持中文等文字、数字、空格、下划线和连字符。")
    members = squad["members"]
    names = {agent.name for agent in agents}
    if (not isinstance(members, list) or not members
            or any(not isinstance(member, str) or member not in names for member in members)
            or len(members) != len(set(members)) or squad["lead"] not in members):
        raise RegistryValidationError("小队成员须为已配置的 Agent，不能重复，并且必须包含队长。")
    return WorkspaceRegistry(agents, Squad(name, squad["lead"], tuple(members)))


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


def update_registry(state, default, payload):
    with _registry_write(state) as path:
        if set(payload) == {"squad"}:
            current = load_registry(state, default).payload()
            registry = parse_registry({**current, "squad": payload["squad"]})
        elif set(payload) == {"agent"}:
            agent, = parse_agents([payload["agent"]])
            current = load_registry(state, default)
            if agent.name not in {item.name for item in current.agents}:
                raise RegistryValidationError("该 Agent 已不在工作区，请刷新后重试。")
            registry = WorkspaceRegistry(
                tuple(agent if item.name == agent.name else item for item in current.agents), current.squad)
        elif set(payload) == {"agents"}:
            current = load_registry(state, default)
            agents = parse_agents(payload["agents"])
            names = tuple(agent.name for agent in agents)
            members = tuple(name for name in current.squad.members if name in names) or names
            lead = current.squad.lead if current.squad.lead in members else members[0]
            registry = WorkspaceRegistry(agents, Squad(current.squad.name, lead, members))
        else:
            registry = parse_registry(payload)
        _write_registry(path, registry)
        return registry


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
