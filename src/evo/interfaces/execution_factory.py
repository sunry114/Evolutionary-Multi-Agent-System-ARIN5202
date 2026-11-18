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
from evo.application.assignment import AssignmentService
from evo.application.executor import ActionExecutor
from evo.application.orchestrator import ArtifactCheck, Orchestrator
from evo.application.planner import Planner
from evo.config import HarnessConfig, HarnessModelConfig, ProviderConfig


def create_orchestrator(state, *, agents, store=None, provider=None,
                        harness_config=None, model=None, credential_loader=None):
    """Build real ports with one state root; tests may inject controlled ports."""
    state = Path(state).absolute()
    store = store or SQLiteStore(state / "events.sqlite3")
    approval = ApprovalService(store)
    provider_config = None
    if provider is None:
        provider_config = ProviderConfig.from_environment()
        provider = create_provider(provider_config)
        # Fail before a task is persisted when credentials are absent.
        load_key(provider_config.provider)
    config = harness_config or HarnessConfig(
        adapter=os.environ.get("EVO_HARNESS_ADAPTER", "claude_code"),
        cli_path=os.environ.get("EVO_CLAUDE_CLI", "claude"))
    if config.adapter == "claude_code" and model is None:
        provider_config = provider_config or ProviderConfig.from_environment()
        endpoint = ("https://api.deepseek.com/anthropic" if provider_config.provider == "deepseek"
                    else provider_config.base_url)
        model = HarnessModelConfig(provider=provider_config.provider, endpoint=endpoint,
                                   model=provider_config.model)
    if config.adapter == "claude_code" and credential_loader is None:
        provider_config = provider_config or ProviderConfig.from_environment()
        credential_loader = lambda: load_key(provider_config.provider)
    workspace_factory = lambda owner: WorkspaceFilesystem(state, owner)
    memory_factory = lambda owner: SQLiteMemoryStore(state, owner)
    token_provider = None
    repository = os.environ.get("EVO_GITHUB_REPOSITORY", "")
    if repository:
        token_provider = RepositoryToken(repository, lambda: os.environ.get("GITHUB_TOKEN", ""))
    coordinator = None

    def harness_factory(task_id, owner, workspace):
        executor = ActionExecutor(store, approval, agent=owner, workspace=workspace,
                                  python=DockerPythonRunner(workspace),
                                  git=DockerGitTools(workspace, token_provider=token_provider))
        def delegate(parent_id, agent, description, path, contains):
            return coordinator.delegate(parent_id, agent, description,
                                        ArtifactCheck(path, contains=contains))
        broker = PlatformToolBroker(executor, web=WebTools(task_id),
                                    artifacts=ArtifactExchange(workspace), delegation=delegate)
        return create_harness(config, state, broker, model=model,
                              credential_loader=credential_loader)

    coordinator = Orchestrator(
        store, Planner(provider, agents, approval),
        AssignmentService(provider, agents, store), harness_factory,
        workspace_factory, agents=agents, memory_factory=memory_factory)
    return coordinator
