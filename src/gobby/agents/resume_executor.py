"""Resume cancelled daemon-stop agent runs from persisted launch metadata."""

from __future__ import annotations

import asyncio
import functools
import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import psycopg

from gobby.agents.codex_oss import (
    codex_local_transport_strategy,
    codex_oss_provider_for_local_endpoint,
)
from gobby.agents.constants import (
    CLAUDE_MANAGED_AGENT_ENV,
    MCP_CONNECT_TIMEOUT_MS,
    MCP_CONNECT_TIMEOUT_MS_VALUE,
    MCP_TIMEOUT,
    MCP_TIMEOUT_VALUE,
)
from gobby.agents.external_write_grants import revalidate_write_grant
from gobby.agents.local_model import (
    LocalModelError,
    ensure_local_model,
    refresh_local_model_context,
)
from gobby.agents.resume_executor_settlement import (
    _fire_resume_started,
    _park_unlaunched_successor,
    _persist_resume_runtime,
    _ResumeRunner,
    _rollback_prepared_resume,
)
from gobby.agents.resume_finalization import (
    finalize_resume_handoff_async,
    notify_parent_of_recovery,
)
from gobby.agents.resume_metadata import (
    filter_resume_config_overrides,
    merge_resume_metadata_env,
)
from gobby.agents.resume_placement import launch_resume
from gobby.agents.sandbox_gate import SandboxRequiredError, resolve_resume_sandbox
from gobby.agents.spawn import prepare_terminal_resume
from gobby.agents.spawn_executor_support import (
    _codex_runtime_config_overrides,
    schedule_codex_prompt_delivery,
)
from gobby.agents.spawners.command_builder import build_cli_command
from gobby.agents.srt_runtime import SrtRuntimeError, prepare_sandbox_launch
from gobby.agents.trust import pre_approve_directory
from gobby.ai.codex_endpoint import (
    codex_endpoint_config_overrides,
    codex_endpoint_env,
)
from gobby.ai.endpoints import resolve_generation_endpoint_selector
from gobby.providers.version_gate import ensure_agy_support
from gobby.sessions.context_usage import local_context_variable_updates
from gobby.storage import daemon_resume_keys
from gobby.storage.agents import AgentRun
from gobby.storage.terminals import Terminal
from gobby.utils.git import run_to_completion
from gobby.utils.machine_id import get_machine_id

logger = logging.getLogger(__name__)

SUPPORTED_RESUME_PROVIDERS = frozenset({"agy", "claude", "qwen", "grok", "codex", "droid"})

# Protocol bookkeeping accumulated on the original run must never leak into a
# successor's launch snapshot: each recovery episode starts clean.
_INHERITED_PROTOCOL_KEYS = (
    daemon_resume_keys.RESUME_PHASE_KEY,
    daemon_resume_keys.CONSUMED_AT_KEY,
    daemon_resume_keys.CONSUMED_BY_KEY,
    daemon_resume_keys.FAILURE_COUNT_KEY,
    daemon_resume_keys.REAP_STARTED_AT_KEY,
    daemon_resume_keys.REAP_REQUESTED_AT_KEY,
    daemon_resume_keys.REAPED_AT_KEY,
    daemon_resume_keys.RECONCILIATION_PENDING_KEY,
    daemon_resume_keys.FINALIZED_AT_KEY,
    daemon_resume_keys.TERMINAL_ID_KEY,
    daemon_resume_keys.SPAWN_KEY_KEY,
    daemon_resume_keys.RESUMED_FROM_RUN_ID_KEY,
)
DAEMON_STOP_CONTINUATION_PROMPT = (
    "Continue the interrupted task after the Gobby daemon stopped. Inspect the current "
    "workspace state, preserve any existing work, and continue from where the prior run left off."
)


class _SessionLookup(Protocol):
    def get(self, session_id: str) -> Any: ...


@dataclass(frozen=True)
class ResumeAgentResult:
    success: bool
    run_id: str | None = None
    child_session_id: str | None = None
    error: str | None = None


async def resume_agent_run(
    original_run: AgentRun,
    *,
    resume_metadata: dict[str, Any],
    runner: _ResumeRunner,
    session_manager: _SessionLookup,
    daemon_config: Any | None = None,
    completion_registry: CompletionEventRegistry | None = None,
    agent_pane_reserver: AgentPaneReserver | None = None,
) -> ResumeAgentResult:
    """Start a provider-native resume process for a daemon-stop run.

    Args:
        original_run: Cancelled daemon-stop agent run being resumed.
        resume_metadata: Persisted launch snapshot from the original run.
        runner: Agent runner exposing child session creation and run storage.
        session_manager: Session lookup used to recover provider-native IDs.
        daemon_config: Optional daemon config used for tmux spawn settings.
        agent_pane_reserver: Daemon pane reserver; a placed snapshot is refused without it.
    """
    from gobby.tasks.agentic_close_review import TASK_CLOSE_REVIEWER_AGENT

    if original_run.agent_name == TASK_CLOSE_REVIEWER_AGENT:
        # Its close review stays bound to this run, so a successor could never
        # submit a verdict; close-review reconciliation retries the review instead.
        return ResumeAgentResult(False, error="resume_task_close_reviewer_unsupported")
    provider = _metadata_str(resume_metadata, "provider") or original_run.provider
    try:
        await asyncio.to_thread(revalidate_write_grant, resume_metadata)
    except ValueError as exc:
        return ResumeAgentResult(False, error=f"resume_external_write_grant_invalid:{exc}")
    if provider not in SUPPORTED_RESUME_PROVIDERS:
        return ResumeAgentResult(False, error=f"resume_unsupported_provider:{provider}")
    if provider == "agy":
        record = await ensure_agy_support()
        if not record.supported:
            return ResumeAgentResult(False, error=record.reason)
    if provider == "droid" and shutil.which("droid") is None:
        return ResumeAgentResult(False, error="droid CLI not found in PATH")

    model_selector = _metadata_str(resume_metadata, "model")
    resume_model = model_selector
    endpoint_config_overrides: tuple[str, ...] = ()
    endpoint_env: dict[str, str] = {}
    codex_oss_provider: str | None = None
    endpoint_api_base: str | None = None
    endpoint_api_token: str | None = None
    local_context_route = None
    local_context_observation = None
    try:
        endpoint_selection = resolve_generation_endpoint_selector(
            daemon_config,
            model_selector,
        )
    except ValueError as exc:
        return ResumeAgentResult(False, error=str(exc))
    if endpoint_selection is not None:
        endpoint = endpoint_selection.endpoint_with_selected_model()
        resume_model = endpoint_selection.selected_model
        if endpoint.wire_api == "responses":
            if provider != "codex":
                return ResumeAgentResult(
                    False,
                    error="Responses generation endpoints require provider='codex'",
                )
            endpoint_config_overrides = codex_endpoint_config_overrides(
                endpoint_selection.name,
                endpoint,
                model=resume_model,
            )
            try:
                endpoint_env.update(codex_endpoint_env(endpoint))
            except ValueError as exc:
                return ResumeAgentResult(False, error=str(exc))
        elif provider == "codex":
            strategy = codex_local_transport_strategy(endpoint.protocol)
            if strategy == "config-override":
                try:
                    resume_model = await ensure_local_model(
                        endpoint, run_manager=runner.run_storage
                    )
                except LocalModelError as exc:
                    return ResumeAgentResult(
                        False,
                        error=f"Local model pre-flight failed: {exc}",
                    )
                endpoint_config_overrides = codex_endpoint_config_overrides(
                    endpoint_selection.name,
                    endpoint,
                    model=resume_model,
                )
                if endpoint.api_key:
                    try:
                        endpoint_env.update(codex_endpoint_env(endpoint))
                    except ValueError as exc:
                        return ResumeAgentResult(False, error=str(exc))
            else:
                codex_oss_provider = codex_oss_provider_for_local_endpoint(endpoint)
        else:
            endpoint_api_base = endpoint.api_base
            endpoint_api_token = endpoint.api_key
        local_context_route, local_context_observation = await refresh_local_model_context(
            endpoint,
            endpoint_name=endpoint_selection.name,
            model=resume_model,
            machine_id=get_machine_id(),
        )

    native_session_id = _provider_native_session_id(
        original_run,
        resume_metadata,
        session_manager=session_manager,
        provider=provider,
    )
    if native_session_id is None:
        return ResumeAgentResult(False, error="provider_native_session_id_missing")

    cwd = _metadata_str(resume_metadata, "cwd") or _metadata_str(resume_metadata, "workspace_path")
    project_id = _metadata_str(resume_metadata, "project_id")
    parent_session_id = _metadata_str(resume_metadata, "parent_session_id")
    if not cwd or not project_id or not parent_session_id:
        return ResumeAgentResult(False, error="resume_metadata_incomplete")
    cwd_path = Path(cwd).expanduser()
    if not cwd_path.is_absolute():
        return ResumeAgentResult(False, error="resume_cwd_not_absolute")
    cwd = str(cwd_path)

    prompt = original_run.continuation_prompt or _metadata_str(
        resume_metadata, "continuation_prompt"
    )
    if not prompt:
        prompt = DAEMON_STOP_CONTINUATION_PROMPT

    run_id = str(uuid.uuid4())
    child_session_id = original_run.child_session_id
    if not child_session_id:
        return ResumeAgentResult(False, error="daemon_stop_child_session_missing")
    metadata = dict(resume_metadata)
    local_context_updates = local_context_variable_updates(
        local_context_route,
        local_context_observation,
    )
    metadata.update(local_context_updates)
    for stale_key in _INHERITED_PROTOCOL_KEYS:
        metadata.pop(stale_key, None)
    metadata[daemon_resume_keys.RESUMED_FROM_RUN_ID_KEY] = original_run.id
    metadata["provider_native_session_id"] = native_session_id
    metadata[daemon_resume_keys.RESUME_PHASE_KEY] = "prepared"

    initial_variables = dict(resume_metadata.get("initial_variables") or {})
    initial_variables.update(local_context_updates)
    initial_variables["daemon_stop_resume"] = True
    initial_variables["resumed_from_agent_run_id"] = original_run.id
    metadata["initial_variables"] = initial_variables

    try:
        config_runtime = getattr(runner, "config_runtime", None)
        config_snapshot = config_runtime.capture().snapshot if config_runtime is not None else None
        spawn_context = prepare_terminal_resume(
            session_manager=runner.child_session_manager,
            credential_manager=runner.run_storage.credential_manager,
            config_snapshot=config_snapshot,
            existing_session_id=child_session_id,
            original_run_id=original_run.id,
            parent_session_id=parent_session_id,
            project_id=project_id,
            source=provider,
            workflow_name=_metadata_str(resume_metadata, "workflow"),
            agent_name=_metadata_str(resume_metadata, "agent_slug") or original_run.agent_name,
            initial_variables=initial_variables,
            git_branch=_metadata_str(resume_metadata, "branch_name"),
            prompt=prompt,
            model=resume_model,
            is_local=bool(getattr(original_run, "is_local", False)),
            max_agent_depth=5,
            agent_run_id=run_id,
            task_id=original_run.task_id,
            claimed_session_id=original_run.claimed_session_id,
            timeout_seconds=original_run.timeout_seconds,
            sandbox_enabled=_sandbox_enabled(metadata),
            requested_reasoning_effort=_metadata_str(
                resume_metadata,
                "requested_reasoning_effort",
            ),
            effective_reasoning_effort=_metadata_str(
                resume_metadata,
                "effective_reasoning_effort",
            ),
            reasoning_required=bool(resume_metadata.get("reasoning_required")),
            reasoning_status=_metadata_str(resume_metadata, "reasoning_status") or "not_requested",
            reasoning_message=_metadata_str(resume_metadata, "reasoning_message"),
            resume_metadata_json=metadata,
            worktree_id=_metadata_str(resume_metadata, "worktree_id"),
            clone_id=_metadata_str(resume_metadata, "clone_id"),
            workspace_path=cwd,
        )
    except (ValueError, psycopg.Error) as exc:
        error = f"resume_preflight_failed:{type(exc).__name__}"
        logger.warning(
            "Failed resume preflight for %s",
            run_id,
            exc_info=True,
            extra={"run_id": run_id, "original_run_id": original_run.id, "error": str(exc)},
        )
        return ResumeAgentResult(False, run_id=run_id, error=error)

    env = merge_resume_metadata_env(resume_metadata.get("env"))
    env.update(spawn_context.env_vars)
    env.update(endpoint_env)
    if endpoint_api_base:
        env[_RESUME_API_BASE_ENV_KEYS[provider]] = endpoint_api_base
    if endpoint_api_token:
        env[_RESUME_API_TOKEN_ENV_KEYS[provider]] = endpoint_api_token
    resume_machine_id = _metadata_str(resume_metadata, "machine_id")
    try:
        env["GOBBY_MACHINE_ID"] = str(uuid.UUID(resume_machine_id)) if resume_machine_id else ""
    except ValueError:
        env["GOBBY_MACHINE_ID"] = ""
    if not env["GOBBY_MACHINE_ID"]:
        env.pop("GOBBY_MACHINE_ID")
    park = functools.partial(
        _park_unlaunched_successor,
        runner,
        original_run=original_run,
        successor_run_id=run_id,
        child_session_id=spawn_context.session_id,
        completion_registry=completion_registry,
    )
    # The successor already exists: a refusal or a cancellation parks it exactly once.
    # A cancelled verifier thread finishes on its own; it reads only the SRT install.
    try:
        sandbox_config = await asyncio.to_thread(resolve_resume_sandbox, resume_metadata)
    except asyncio.CancelledError:
        await run_to_completion(park())
        raise
    except SandboxRequiredError:
        await run_to_completion(park())
        return ResumeAgentResult(False, run_id=run_id, error="sandbox_required")
    daemon_port = int(getattr(daemon_config, "daemon_port", 60887))
    websocket = getattr(daemon_config, "websocket", None)
    websocket_port = int(getattr(websocket, "port", 60888))
    try:
        launch = await prepare_sandbox_launch(
            config=sandbox_config,
            provider=provider,
            workspace_path=cwd,
            run_id=run_id,
            resolver=None,
            daemon_port=daemon_port,
            websocket_port=websocket_port,
            api_base=_resume_api_base(provider, env),
            env=env,
            allow_run_unix_sockets=True,
        )
    except (OSError, ValueError, SrtRuntimeError) as exc:
        error = f"resume_sandbox_failed_closed:{type(exc).__name__}:{exc}"
        await _rollback_prepared_resume(
            runner,
            original_run_id=original_run.id,
            successor_run_id=run_id,
            child_session_id=spawn_context.session_id,
        )
        return ResumeAgentResult(False, run_id=run_id, error=error)
    env.update(launch.provider_env)
    update_sandbox_enabled = getattr(runner.child_session_manager, "update_sandbox_enabled", None)
    if callable(update_sandbox_enabled):
        update_sandbox_enabled(spawn_context.session_id, launch.enforced)
    update_policy_hash = getattr(
        runner.child_session_manager,
        "update_sandbox_policy_hash",
        None,
    )
    if launch.policy_hash and callable(update_policy_hash):
        update_policy_hash(spawn_context.session_id, launch.policy_hash)

    sandbox_args = launch.provider_args
    config_overrides = list(
        dict.fromkeys(
            [
                # Replay only allowlisted non-secret overrides; the fresh
                # capability token comes from spawn_context.env_vars, minted
                # for this resume, never from a stored override.
                *filter_resume_config_overrides(_str_list(resume_metadata.get("config_overrides"))),
                *endpoint_config_overrides,
            ]
        )
    )
    if provider == "codex":
        # The successor has a new run ID, capability, and sandbox temp root.
        # Replaying the original environment would bind MCP to its ended run.
        config_overrides = [
            override
            for override in config_overrides
            if not override.partition("=")[0].startswith(
                (
                    "mcp_servers.gobby.env.",
                    "mcp_servers.gobby.env_vars",
                    "shell_environment_policy.set.",
                )
            )
        ]
        config_overrides.extend(
            _codex_runtime_config_overrides(launch.provider_env.get("TMPDIR"), env)
        )
    command, _cmd_env = build_cli_command(
        cli=provider,
        # Claude appends its prompt after MCP flags below. Codex TUI receives
        # a post-launch composer paste.
        prompt=None if provider in {"claude", "codex"} else prompt,
        resume_session_id=native_session_id,
        auto_approve=bool(resume_metadata.get("auto_approve", True)),
        working_directory=cwd if provider in {"agy", "codex", "droid", "grok"} else None,
        sandbox_args=None if provider == "claude" else sandbox_args,
        model=resume_model,
        codex_oss_provider=codex_oss_provider,
        reasoning_effort=_metadata_str(resume_metadata, "effective_reasoning_effort"),
        config_overrides=config_overrides,
    )
    launch_updates: dict[str, Any] = {}
    if provider == "claude":
        env.update(CLAUDE_MANAGED_AGENT_ENV)
        env[MCP_TIMEOUT] = MCP_TIMEOUT_VALUE
        claude_mcp_path = _metadata_str(resume_metadata, "mcp_path")
        strict_mcp = bool(resume_metadata.get("strict_mcp"))
        if not claude_mcp_path:
            claude_mcp_config = Path(cwd) / ".mcp.json"
            if claude_mcp_config.exists():
                claude_mcp_path = str(claude_mcp_config)
                strict_mcp = True
                launch_updates["mcp_path"] = claude_mcp_path
                launch_updates["strict_mcp"] = strict_mcp
        if claude_mcp_path:
            env[MCP_CONNECT_TIMEOUT_MS] = MCP_CONNECT_TIMEOUT_MS_VALUE
            command.extend(["--mcp-config", claude_mcp_path])
            if strict_mcp:
                command.append("--strict-mcp-config")
        command.extend(sandbox_args)
        command.append(prompt)
    # Merge only the launch-snapshot keys refreshed above. The full local
    # metadata dict carries protocol keys from a stale read (phase, native
    # session id); re-merging it could reset a concurrently advanced phase.
    launch_updates["env"] = merge_resume_metadata_env(env)
    launch_updates["sandbox_args"] = list(launch.provider_args)
    launch_updates["sandbox_env"] = dict(launch.provider_env)
    launch_updates["sandbox"] = launch.metadata()
    launch_updates["config_overrides"] = config_overrides
    runner.run_storage.merge_resume_metadata(run_id, launch_updates)
    launched = runner.run_storage.transition_resume_phase(
        run_id,
        expected_phase="prepared",
        new_phase="launch_requested",
    )
    if launched is None:
        current = runner.run_storage.get(run_id)
        if current is not None and (
            current.status in {"running", "success", "error", "timeout", "cancelled"}
            or (current.resume_metadata_json or {}).get("daemon_stop_resume_phase") == "finalized"
        ):
            return ResumeAgentResult(
                True,
                run_id=run_id,
                child_session_id=spawn_context.session_id,
            )
        await _rollback_prepared_resume(
            runner,
            original_run_id=original_run.id,
            successor_run_id=run_id,
            child_session_id=spawn_context.session_id,
        )
        return ResumeAgentResult(False, run_id=run_id, error="resume_launch_phase_cas_failed")

    pre_approve_directory(provider, cwd)
    from gobby.agents.spawn_executor import _runtime_spawn
    from gobby.agents.spawn_executor_providers import ProviderSpawnPlan
    from gobby.agents.spawn_models import SpawnRequest

    plan = ProviderSpawnPlan(
        command=command,
        env=env,
        launch=launch,
        auth_cli=provider,
        child_session_id=spawn_context.session_id,
        agent_run_id=run_id,
        title=f"gobby-resume-{run_id}",
        codex_prompt=prompt if provider == "codex" else None,
    )
    spawn_request = SpawnRequest(
        prompt=prompt,
        cwd=cwd,
        provider=provider,
        session_id=spawn_context.session_id,
        run_id=run_id,
        parent_session_id=parent_session_id,
        project_id=project_id,
        agent_run_id=run_id,
        session_manager=runner.child_session_manager,
        run_manager=runner.run_storage,
        auto_approve=bool(resume_metadata.get("auto_approve", True)),
        provider_args=(),
        sandbox_config=sandbox_config,
        daemon_config=daemon_config,
        prepared_spawn=spawn_context,
        terminal_manager=getattr(runner, "terminal_manager", None),
        terminal_runtime_registry=getattr(runner, "terminal_runtime_registry", None),
    )
    try:
        terminal_result = await launch_resume(
            spawn_request,
            plan,
            runtime_spawn=_runtime_spawn,
            snapshot=resume_metadata.get("placement"),
            reserver=agent_pane_reserver,
            sessions=session_manager,
            parent_session_id=parent_session_id,
            project_id=project_id,
            worktree_id=_metadata_str(resume_metadata, "worktree_id"),
            park=park,
        )
    except Exception as exc:
        error = f"resume_spawn_failed:{type(exc).__name__}:{exc}"
        await park()
        return ResumeAgentResult(False, run_id=run_id, error=error)
    if not terminal_result.success:
        error = terminal_result.error or terminal_result.message or "resume_spawn_failed"
        await park()
        return ResumeAgentResult(False, run_id=run_id, error=error)

    terminal: Terminal | None = None
    if terminal_result.terminal_id:
        manager = getattr(runner, "terminal_manager", None)
        if manager is not None:
            terminal = await asyncio.to_thread(manager.get, terminal_result.terminal_id)

    if provider == "codex" and terminal is not None:
        coordinator = getattr(runner, "write_coordinator", None)
        if coordinator is not None:
            schedule_codex_prompt_delivery(
                coordinator,
                terminal,
                prompt,
                run_id,
                runner.run_storage,
            )
    runner.run_storage.merge_resume_metadata(
        run_id,
        {
            daemon_resume_keys.TERMINAL_ID_KEY: terminal_result.terminal_id,
            daemon_resume_keys.SPAWN_KEY_KEY: terminal.spawn_key if terminal is not None else None,
        },
    )
    try:
        started_run = _persist_resume_runtime(
            runner,
            run_id,
            pid=terminal_result.pid,
            terminal_id=terminal_result.terminal_id,
            worktree_id=_metadata_str(resume_metadata, "worktree_id"),
            clone_id=_metadata_str(resume_metadata, "clone_id"),
        )
    except Exception as exc:
        error = f"resume_runtime_persist_failed:{type(exc).__name__}"
        logger.warning(
            "Leaving live provisional successor %s for reconciliation: %s",
            run_id,
            error,
            exc_info=True,
        )
        return ResumeAgentResult(
            True,
            run_id=run_id,
            child_session_id=spawn_context.session_id,
            error=error,
        )
    if started_run is None:
        current = runner.run_storage.get(run_id)
        if current is None:
            return ResumeAgentResult(False, run_id=run_id, error="agent_run_missing_after_spawn")
        if current.status not in {"running", "success", "error", "timeout", "cancelled"}:
            return ResumeAgentResult(
                True,
                run_id=run_id,
                child_session_id=spawn_context.session_id,
                error="agent_run_start_reconciliation_pending",
            )

    try:
        await finalize_resume_handoff_async(
            runner.run_storage.db,
            original_run_id=original_run.id,
            successor_run_id=run_id,
            child_session_id=spawn_context.session_id,
            completion_registry=completion_registry,
        )
    except (ValueError, psycopg.Error) as exc:
        logger.warning(
            "Leaving successor %s for handoff finalization retry: %s",
            run_id,
            exc,
        )
        return ResumeAgentResult(
            True,
            run_id=run_id,
            child_session_id=spawn_context.session_id,
            error=f"resume_finalization_pending:{type(exc).__name__}",
        )

    latest = runner.run_storage.get(run_id)
    if latest is not None and latest.status in {"success", "error", "timeout", "cancelled"}:
        return ResumeAgentResult(
            True,
            run_id=run_id,
            child_session_id=spawn_context.session_id,
        )
    _fire_resume_started(
        original_run,
        run_id,
        provider,
        terminal_result,
        terminal,
        parent_session_id,
    )
    await asyncio.to_thread(
        notify_parent_of_recovery,
        runner.run_storage.db,
        child_session_id=spawn_context.session_id,
        parent_session_id=parent_session_id,
        content=f"Child agent relaunched after daemon restart as run {run_id}; session state preserved.",
        run_id=run_id,
        event="agent_relaunched",
    )
    return ResumeAgentResult(True, run_id=run_id, child_session_id=spawn_context.session_id)


def _provider_native_session_id(
    run: AgentRun,
    metadata: dict[str, Any],
    *,
    session_manager: Any,
    provider: str,
) -> str | None:
    for value in (
        _metadata_str(metadata, "provider_native_session_id"),
        run.sdk_session_id,
    ):
        if value:
            return value
    if not run.child_session_id:
        return None
    try:
        session = session_manager.get(run.child_session_id)
    except LookupError:
        return None
    value = getattr(session, "external_id", None)
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("agent-"):
        return None
    if value == run.child_session_id and provider != "claude":
        return None
    return value


def _metadata_str(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) and value else None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [item for item in value if isinstance(item, str)]


_RESUME_API_BASE_ENV_KEYS = {
    "claude": "ANTHROPIC_BASE_URL",
    "codex": "OPENAI_BASE_URL",
    "droid": "FACTORY_API_BASE_URL",
    "grok": "GROK_API_BASE",
    "qwen": "QWEN_API_BASE",
}
_RESUME_API_TOKEN_ENV_KEYS = {
    "claude": "ANTHROPIC_AUTH_TOKEN",
    "codex": "OPENAI_API_KEY",
    "droid": "FACTORY_API_KEY",
    "grok": "XAI_API_KEY",
    "qwen": "QWEN_API_KEY",
}


def _resume_api_base(provider: str, env: dict[str, str]) -> str | None:
    key = _RESUME_API_BASE_ENV_KEYS.get(provider)
    return env.get(key) if key else None


def _sandbox_enabled(metadata: dict[str, Any]) -> bool:
    sandbox_config = metadata.get("sandbox_config")
    return isinstance(sandbox_config, dict) and bool(sandbox_config.get("enabled"))


if TYPE_CHECKING:
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.terminals.workspace_agent_panes import AgentPaneReserver
