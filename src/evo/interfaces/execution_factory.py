"""Trusted wiring for the C11 coordinator and one scoped Harness per task."""

from __future__ import annotations

import os
from dataclasses import asdict, replace
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
from evo.application.assignment import AssignmentService, RoutingService, TaskAssignmentService
from evo.application.executor import ActionExecutor
from evo.application.orchestrator import ArtifactCheck, Orchestrator
from evo.application.planner import Planner
from evo.config import HarnessConfig, HarnessModelConfig, ProviderConfig
from evo.domain.policies import DEFAULT_LIMITS, team_coordinator


def create_orchestrator(state, *, agents, store=None, provider=None,
                        harness_config=None, model=None, credential_loader=None):
    """Build real ports with one state root; tests may inject controlled ports."""
    state = Path(state).absolute()
    store = store or SQLiteStore(state / "events.sqlite3")
    agents = tuple(agents)
    approval = ApprovalService(store, clarification_agent=team_coordinator(agents))
    provider_config = None
    if provider is None:
        provider_config = ProviderConfig.from_environment()
        provider = create_provider(provider_config)
        # Fail before a task is persisted when credentials are absent.
        load_key(provider_config.provider)
    config = harness_config or HarnessConfig(
        adapter=os.environ.get("EVO_HARNESS_ADAPTER", "provider"),
        timeout=float(os.environ.get("EVO_HARNESS_TIMEOUT", "600")),
        cli_path=os.environ.get("EVO_CLAUDE_CLI", "claude"),
        max_tool_calls=int(os.environ.get("EVO_MAX_TOOL_CALLS", "80")))
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

    def saved_execution(task_id):
        try:
            task = store.get_task(task_id)
        except KeyError:
            return {}
        while task["parent_id"]:
            task = store.get_task(task["parent_id"])
        configs = [e["payload"] for e in store.list_events(task["id"])
                   if e["event_type"] == "run_config"]
        if configs and "execution" not in configs[-1]:
            # Pre-refactor runs used agent workspaces and bidding. Infer their
            # adapter from trusted audit events rather than the new default.
            rows = (task,) + tuple(store.list_children(task["id"]))
            adapters = [e["payload"]["adapter"] for row in rows
                        for e in store.list_events(row["id"])
                        if e["event_type"] == "harness_started" and "adapter" in e["payload"]]
            legacy = replace(config, adapter=adapters[-1] if adapters else "claude_code")
            return {"harness": asdict(legacy), "workspace_scope": "agent", "assignment": "bidding"}
        return configs[-1].get("execution", {}) if configs else {}

    def scoped_workspace(owner, task_id):
        scope = saved_execution(task_id).get("workspace_scope")
        if scope == "task":
            task = store.get_task(task_id)
            while task["parent_id"]:
                task = store.get_task(task["parent_id"])
            return WorkspaceFilesystem(state, owner, task_scope=task["id"])
        return workspace_factory(owner)

    def harness_factory(task_id, owner, workspace):
        execution = saved_execution(task_id)
        effective = HarnessConfig(**execution["harness"]) if execution.get("harness") else config
        effective_model = HarnessModelConfig(**execution["model"]) if execution.get("model") else model
        effective_loader = credential_loader
        if effective.adapter == "claude_code" and effective_model is None:
            selected = ProviderConfig.from_environment()
            effective_model = HarnessModelConfig(
                provider=selected.provider, model=selected.model,
                endpoint="https://api.deepseek.com/anthropic" if selected.provider == "deepseek" else selected.base_url)
        if effective.adapter == "claude_code" and effective_loader is None:
            effective_loader = lambda: load_key(effective_model.provider)
        executor = ActionExecutor(store, approval, agent=owner, workspace=workspace,
                                  python=DockerPythonRunner(workspace),
                                  git=DockerGitTools(workspace, token_provider=token_provider))
        def delegate(parent_id, agent, description, path, contains):
            return coordinator.delegate(parent_id, agent, description,
                                        ArtifactCheck(path, contains=contains))
        broker = PlatformToolBroker(executor, web=WebTools(task_id),
                                    artifacts=ArtifactExchange(workspace),
                                    delegation=delegate if effective.adapter == "baseline" else None)
        return create_harness(effective, state, broker, model=effective_model,
                              credential_loader=effective_loader, provider=provider)

    strategy = os.environ.get("EVO_ASSIGNMENT_STRATEGY",
                              "bidding" if config.adapter == "baseline" else "routing")
    if strategy not in ("routing", "bidding"):
        raise ValueError("Unknown assignment strategy")
    assignment = TaskAssignmentService(
        {"bidding": AssignmentService(provider, agents, store), "routing": RoutingService(agents, store)},
        lambda task_id: saved_execution(task_id).get("assignment", strategy))

    def discussion_context(task_id):
        # Only public team decisions enter planning; private answers stay in DMs.
        records = [m for m in store.list_task_messages(task_id)
                   if m["kind"] == "group" and m.get("recipient") == "chat" and m["sender"] != "human"]
        context, size = [], 0
        for record in reversed(records):
            size += len(record["content"].encode("utf-8"))
            if size > 32768:
                break
            context.append({"agent": record["sender"], "content": record["content"]})
        return list(reversed(context))

    coordinator = Orchestrator(
        store, Planner(provider, agents, approval, discussion_loader=discussion_context, events=store),
        assignment, harness_factory,
        workspace_factory, agents=agents, memory_factory=memory_factory,
        limits=replace(DEFAULT_LIMITS, max_steps_per_task=config.max_tool_calls),
        scoped_workspace_factory=scoped_workspace, handoff_factory=ArtifactExchange,
        execution_config={"harness": asdict(config), "assignment": strategy,
                          "decision_protocol": (("native_functions" if getattr(provider, "native_tools", False) is True
                                                 else "json_actions") if config.adapter == "provider" else config.adapter),
                          "model": asdict(model) if model else None,
                          "provider": ({key: getattr(provider_config, key)
                                        for key in ("provider", "model", "base_url", "timeout", "retries")}
                                       if provider_config else None),
                          "workspace_scope": "agent" if config.adapter == "baseline" else "task"})
    return coordinator
