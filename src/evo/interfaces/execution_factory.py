"""Trusted wiring for the C11 coordinator and one scoped Harness per task."""

from __future__ import annotations

import os
from pathlib import Path

from evo.adapters.harness import create_harness
from evo.adapters.harness.mcp_bridge import PlatformToolBroker
from evo.adapters.llm.credentials import load_key
from evo.adapters.llm.factory import create_provider
from evo.adapters.persistence.sqlite_memory import SQLiteMemoryStore
from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.adapters.tools.artifacts import ArtifactExchange
from evo.adapters.tools.docker import DockerPythonRunner
from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.adapters.tools.git import DockerGitTools, RepositoryToken
from evo.adapters.tools.web import WebTools
from evo.application.approvals import ApprovalService
from evo.application.executor import ActionExecutor
from evo.application.orchestrator import ArtifactCheck, Orchestrator
from evo.application.planner import Planner
from evo.config import HarnessConfig, HarnessModelConfig, ProviderConfig
from evo.ports.harness import ToolInputError


def create_orchestrator(state, *, agents, squad=None, direct_assignee=None, store=None, provider=None,
                        harness_config=None, model=None, credential_loader=None, memory_state=None,
                        provider_config=None):
    """Build scoped execution ports for the saved assignee and roster."""
    agents = tuple(agents)
    by_name = {agent.name: agent for agent in agents}
    if (squad is None) == (direct_assignee is None):
        raise ValueError("Choose one Squad or directly assigned member")
    if squad is not None and set(squad["members"]) != by_name.keys():
        raise ValueError("Squad routing requires the saved member roster")
    routing_actor = squad["lead"] if squad is not None else direct_assignee
    if routing_actor not in by_name:
        raise ValueError("Routing requires a known captain or directly assigned member")
    state = Path(state).absolute()
    store = store or SQLiteStore(state / "events.sqlite3")
    approval = ApprovalService(store)
    if provider is None:
        provider_config = provider_config or ProviderConfig.from_environment()
        provider = create_provider(provider_config)
        # Fail before claiming a start when credentials are absent.
        load_key(provider_config.provider)
    config = harness_config or HarnessConfig(
        adapter=os.environ.get("EVO_HARNESS_ADAPTER", "claude_code"),
        cli_path=os.environ.get("EVO_CLAUDE_CLI", "claude"))
    if config.adapter == "claude_code" and model is None:
        provider_config = provider_config or ProviderConfig.from_environment()
        endpoint = ("https://api.deepseek.com/anthropic" if provider_config.provider == "deepseek"
                    else provider_config.base_url)
        model = HarnessModelConfig(provider=provider_config.provider, endpoint=endpoint,
                                   model=provider_config.model, evaluation=provider_config.evaluation)
    if config.adapter == "claude_code" and credential_loader is None:
        provider_config = provider_config or ProviderConfig.from_environment()
        credential_loader = lambda: load_key(provider_config.provider)
    workspace_factory = lambda owner: WorkspaceFilesystem(state, owner)
    memory_root = state if memory_state is None else Path(memory_state).absolute()
    memory_factory = lambda owner: SQLiteMemoryStore(memory_root, owner)
    token_provider = None
    repository = os.environ.get("EVO_GITHUB_REPOSITORY", "")
    if repository:
        token_provider = RepositoryToken(repository, lambda: os.environ.get("GITHUB_TOKEN", ""))
    coordinator = None

    def harness_factory(task_id, owner, workspace):
        executor = ActionExecutor(store, approval, agent=owner, workspace=workspace,
                                  python=DockerPythonRunner(workspace),
                                  git=DockerGitTools(workspace, token_provider=token_provider))
        def delegate(parent_id, sender, agent, description, path, contains, *, request_id=None):
            try:
                return coordinator.request_delegation(parent_id, sender, agent, description,
                                                      ArtifactCheck(path, contains=contains), request_id=request_id)
            except ValueError as error:
                raise ToolInputError(str(error)) from None
        broker = PlatformToolBroker(executor, web=WebTools(task_id),
                                    artifacts=ArtifactExchange(workspace), delegation=delegate)
        return create_harness(config, state, broker, model=model,
                              credential_loader=credential_loader)

    coordinator = Orchestrator(
        store, Planner(provider, agents, approval, actor=by_name[routing_actor]),
        harness_factory, workspace_factory, agents=agents,
        negotiation_provider=provider,
        squad=squad, direct_assignee=direct_assignee, memory_factory=memory_factory)
    return coordinator
