"""Provider command/env preparation for execute_spawn.

These functions build a provider command and sandbox launch. They do not wrap
the command and do not talk to a terminal backend — wrap and dispatch live in
spawn_executor._runtime_spawn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from gobby.agents.constants import (
    GOBBY_AGENT_API_TOKEN,
    GOBBY_AGENT_RUN_ID,
    GOBBY_PROJECT_ID,
    GOBBY_SESSION_ID,
)
from gobby.agents.isolation_code_index import ensure_isolation_code_index
from gobby.agents.sandbox_resolvers import get_sandbox_resolver
from gobby.agents.spawn import PreparedSpawn, build_cli_command
from gobby.agents.spawn_cache_policy import (
    sandbox_config_for_spawn as _sandbox_config_for_spawn,
)
from gobby.agents.spawn_executor_support import (
    _apply_extra_env,
    _codex_mcp_config_overrides,
    _record_actual_sandbox_enforcement,
    _record_resume_launch_details,
    _session_manager_validation_error,
)
from gobby.agents.spawn_models import SpawnRequest, SpawnResult
from gobby.agents.srt_runtime import (
    SandboxLaunch,
    SrtRuntimeError,
    prepare_sandbox_launch,
)
from gobby.agents.trust import pre_approve_directory

logger = logging.getLogger(__name__)

_CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS = ["Workflow", "Task"]
_NATIVE_SUBAGENT_RESEARCH_AGENTS = frozenset({"plan-adversary", "plan-adversary-taskless"})


@dataclass
class ProviderSpawnPlan:
    """Provider-prepared command payload awaiting SRT wrap and runtime dispatch."""

    command: list[str]
    env: dict[str, str]
    launch: SandboxLaunch
    auth_cli: str
    child_session_id: str
    agent_run_id: str
    title: str | None = None
    codex_prompt: str | None = None
    inject_persona: bool = False


def _append_code_index_warning(prompt: str, warning: dict[str, str]) -> str:
    message = warning.get("message", "unknown")
    return (
        f"{prompt}\n\n---\n\n"
        "## Code Index\n"
        "Use standard file search and read tools for code navigation in this isolated "
        f"workspace. Code-index preflight failed: {message}"
    )


def _agent_prompt_prefix(request: SpawnRequest) -> str:
    """Resolve the spawned agent's execution preamble for prompt assembly.

    Codex delivers hook-injected context after the composer prompt, so the
    agent block used to trail the task prompt (#20451). Prepending it at
    spawn assembly puts identity before the task. Only an explicitly requested
    agent is resolved here — activation may pick a configured default agent,
    and guessing it at spawn time risks front-loading the wrong persona.
    """
    if request.session_manager is None:
        return ""
    agent_name = request.agent_name or (request.initial_variables or {}).get("_agent_type")
    if not agent_name:
        return ""
    try:
        from gobby.workflows.agent_resolver import resolve_agent

        agent_body = resolve_agent(
            str(agent_name),
            request.session_manager._storage.db,
            project_id=request.project_id,
        )
    except Exception:
        logger.debug("Prompt resolution failed for spawn agent %r", agent_name, exc_info=True)
        return ""
    if not agent_body:
        return ""
    return agent_body.prompt_for("agent") or ""


async def _prepare_provider_sandbox(
    request: SpawnRequest,
    spawn_context: PreparedSpawn,
    provider: str,
    env: dict[str, str],
) -> SandboxLaunch | SpawnResult:
    config = _sandbox_config_for_spawn(request.sandbox_config, env)
    if config is None:
        launch = SandboxLaunch(backend="provider-native", enforced=False)
        _record_actual_sandbox_enforcement(request, spawn_context, launch)
        return launch
    resolver = None
    if config.enabled and config.backend == "provider-native":
        resolver = get_sandbox_resolver(provider)
    daemon_port = int(getattr(request.daemon_config, "daemon_port", 60887))
    websocket = getattr(request.daemon_config, "websocket", None)
    websocket_port = int(getattr(websocket, "port", 60888))
    try:
        launch = await prepare_sandbox_launch(
            config=config,
            provider=provider,
            workspace_path=request.cwd,
            run_id=spawn_context.agent_run_id,
            resolver=resolver,
            daemon_port=daemon_port,
            websocket_port=websocket_port,
            api_base=request.api_base,
            env=env,
        )
    except (OSError, ValueError, SrtRuntimeError) as exc:
        error = f"Sandbox startup failed closed for {provider}: {exc}"
        if request.run_manager is not None:
            request.run_manager.fail(spawn_context.agent_run_id, error)
        return SpawnResult(
            success=False,
            run_id=spawn_context.agent_run_id,
            child_session_id=spawn_context.session_id,
            status="failed",
            error=error,
        )
    env.update(launch.provider_env)
    _record_actual_sandbox_enforcement(request, spawn_context, launch)
    return launch


async def _prepare_managed_code_index(
    request: SpawnRequest,
    spawn_context: PreparedSpawn,
) -> SpawnResult | None:
    mode = request.code_index_preflight_mode
    if mode is None:
        return None
    try:
        credential = spawn_context.managed_credential
        if credential is None:
            raise RuntimeError("managed credential unavailable for code index preflight")
        # gcore's effective-config resolution requires the spawned run's
        # managed-execution identity; the daemon's own env carries none. The
        # machine id signs the probe grant's principal, which gcode checks
        # against the local machine before using the credential.
        identity_env = {
            name: value
            for name, value in spawn_context.env_vars.items()
            if name in (GOBBY_AGENT_RUN_ID, GOBBY_PROJECT_ID, GOBBY_SESSION_ID) and value
        }
        if request.machine_id:
            identity_env["GOBBY_MACHINE_ID"] = request.machine_id
        # Isolation gcode uses the run-scoped managed credential; the operator
        # token is only a tokenless-dev fallback when no run token was minted.
        run_api_token = spawn_context.env_vars.get(GOBBY_AGENT_API_TOKEN)
        preflight = await ensure_isolation_code_index(
            request.cwd,
            credential=credential,
            api_token=run_api_token or request.code_index_api_token,
            identity_env=identity_env,
        )
        spawn_context.env_vars.update(preflight.env)
        return None
    except Exception as exc:
        if mode == "required":
            return SpawnResult(
                success=False,
                run_id=request.run_id,
                child_session_id=spawn_context.session_id,
                status="failed",
                error=f"planner_code_index_unavailable:{exc}",
            )
        warning = {
            "preflight": "code_index",
            "cwd": request.cwd,
            "message": str(exc),
        }
        request.code_index_preflight_warning = warning
        request.prompt = _append_code_index_warning(request.prompt, warning)
        if request.initial_variables is not None:
            skills = request.initial_variables.get("additional_skills", [])
            if isinstance(skills, list):
                request.initial_variables["additional_skills"] = [
                    skill for skill in skills if skill != "code-index"
                ]
            request.initial_variables["code_index_preflight_warning"] = warning
            request.initial_variables["prompt"] = request.prompt
            if request.session_manager is not None:
                from gobby.workflows.state_manager import SessionVariableManager

                SessionVariableManager(request.session_manager._storage.db).merge_variables(
                    spawn_context.session_id,
                    request.initial_variables,
                )
        logging.getLogger(__name__).warning(
            "Continuing spawn after scoped code index preflight failed for cwd=%s: %s",
            request.cwd,
            exc,
        )
        return None


async def prepare_claude_spawn(request: SpawnRequest) -> ProviderSpawnPlan | SpawnResult:
    if validation_error := _session_manager_validation_error(request, "Claude"):
        return validation_error
    spawn_context = request.prepared_spawn
    gobby_session_id = spawn_context.session_id
    cmd, _cmd_env = build_cli_command(
        cli="claude",
        prompt=None,
        session_id=gobby_session_id,
        auto_approve=True,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
        disallowed_tools=_CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS,
    )
    claude_mcp_config_path = Path(request.cwd) / ".mcp.json"
    claude_mcp_config_arg: str | None = None
    strict_mcp = False
    if claude_mcp_config_path.exists():
        claude_mcp_config_arg = str(claude_mcp_config_path)
        strict_mcp = True
        cmd.extend(["--mcp-config", claude_mcp_config_arg, "--strict-mcp-config"])
    if preflight_error := await _prepare_managed_code_index(request, spawn_context):
        return preflight_error
    env = spawn_context.env_vars.copy()
    _apply_extra_env(env, request)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    if request.api_base:
        env["ANTHROPIC_BASE_URL"] = request.api_base
    if request.api_token:
        env["ANTHROPIC_AUTH_TOKEN"] = request.api_token
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id
    sandbox_result = await _prepare_provider_sandbox(request, spawn_context, "claude", env)
    if isinstance(sandbox_result, SpawnResult):
        return sandbox_result
    launch = sandbox_result
    if launch.enforced and launch.backend == "srt":
        cmd.extend(["--settings", '{"sandbox": {"enabled": false}}'])
    cmd.extend(launch.provider_args)
    if request.prompt:
        cmd.append(request.prompt)
    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=launch.provider_args,
        sandbox_env=launch.provider_env,
        env=env,
        mcp_path=claude_mcp_config_arg,
        strict_mcp=strict_mcp,
        sandbox_launch=launch,
    )
    pre_approve_directory("claude", request.cwd)
    return ProviderSpawnPlan(
        command=cmd,
        env=env,
        launch=launch,
        auth_cli="claude",
        child_session_id=gobby_session_id,
        agent_run_id=spawn_context.agent_run_id,
        title=f"gobby-claude-d{request.agent_depth}",
    )


async def prepare_qwen_spawn(request: SpawnRequest) -> ProviderSpawnPlan | SpawnResult:
    if validation_error := _session_manager_validation_error(request, "Qwen"):
        return validation_error
    spawn_context = request.prepared_spawn
    gobby_session_id = spawn_context.session_id
    if preflight_error := await _prepare_managed_code_index(request, spawn_context):
        return preflight_error
    env = spawn_context.env_vars.copy()
    _apply_extra_env(env, request)
    if request.api_base:
        env["QWEN_API_BASE"] = request.api_base
    if request.api_token:
        env["QWEN_API_KEY"] = request.api_token
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id
    sandbox_result = await _prepare_provider_sandbox(request, spawn_context, "qwen", env)
    if isinstance(sandbox_result, SpawnResult):
        return sandbox_result
    launch = sandbox_result
    cmd, _cmd_env = build_cli_command(
        cli="qwen",
        prompt=request.prompt,
        auto_approve=True,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
        sandbox_args=launch.provider_args or None,
    )
    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=launch.provider_args,
        sandbox_env=launch.provider_env,
        env=env,
        sandbox_launch=launch,
    )
    pre_approve_directory("qwen", request.cwd)
    return ProviderSpawnPlan(
        command=cmd,
        env=env,
        launch=launch,
        auth_cli="qwen",
        child_session_id=gobby_session_id,
        agent_run_id=spawn_context.agent_run_id,
        title=f"gobby-qwen-d{request.agent_depth}",
    )


async def prepare_grok_spawn(request: SpawnRequest) -> ProviderSpawnPlan | SpawnResult:
    if validation_error := _session_manager_validation_error(request, "Grok"):
        return validation_error
    spawn_context = request.prepared_spawn
    gobby_session_id = spawn_context.session_id
    if preflight_error := await _prepare_managed_code_index(request, spawn_context):
        return preflight_error
    env = spawn_context.env_vars.copy()
    _apply_extra_env(env, request)
    if request.api_base:
        env["GROK_API_BASE"] = request.api_base
    if request.api_token:
        env["XAI_API_KEY"] = request.api_token
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id
    sandbox_result = await _prepare_provider_sandbox(request, spawn_context, "grok", env)
    if isinstance(sandbox_result, SpawnResult):
        return sandbox_result
    launch = sandbox_result
    cmd, _cmd_env = build_cli_command(
        cli="grok",
        prompt=request.prompt,
        auto_approve=True,
        working_directory=request.cwd,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
        sandbox_args=launch.provider_args or None,
    )
    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=launch.provider_args,
        sandbox_env=launch.provider_env,
        env=env,
        sandbox_launch=launch,
    )
    pre_approve_directory("grok", request.cwd)
    return ProviderSpawnPlan(
        command=cmd,
        env=env,
        launch=launch,
        auth_cli="grok",
        child_session_id=gobby_session_id,
        agent_run_id=spawn_context.agent_run_id,
        title=f"gobby-grok-d{request.agent_depth}",
    )


async def prepare_codex_spawn(request: SpawnRequest) -> ProviderSpawnPlan | SpawnResult:
    if validation_error := _session_manager_validation_error(request, "Codex"):
        return validation_error
    spawn_context = request.prepared_spawn
    gobby_session_id = spawn_context.session_id
    if preflight_error := await _prepare_managed_code_index(request, spawn_context):
        return preflight_error
    env = spawn_context.env_vars.copy()
    _apply_extra_env(env, request)
    if request.api_base:
        env["OPENAI_BASE_URL"] = request.api_base
    if request.api_token:
        env["OPENAI_API_KEY"] = request.api_token
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id
    sandbox_result = await _prepare_provider_sandbox(request, spawn_context, "codex", env)
    if isinstance(sandbox_result, SpawnResult):
        return sandbox_result
    launch = sandbox_result
    config_overrides = [
        *_codex_mcp_config_overrides(
            request.project_path,
            (launch.provider_env or {}).get("TMPDIR"),
            managed_identity_env=env,
        ),
        *request.codex_config_overrides,
    ]
    if launch.enforced and launch.backend == "srt":
        config_overrides.append('sandbox_mode="danger-full-access"')
    cmd, _cmd_env = build_cli_command(
        cli="codex",
        prompt="",
        auto_approve=True,
        working_directory=request.cwd,
        model=request.model,
        codex_oss_provider=request.codex_oss_provider,
        reasoning_effort=request.effective_reasoning_effort,
        sandbox_args=launch.provider_args or None,
        config_overrides=config_overrides,
    )
    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=launch.provider_args,
        sandbox_env=launch.provider_env,
        env=env,
        config_overrides=config_overrides,
        sandbox_launch=launch,
    )
    pre_approve_directory("codex", request.cwd)
    prompt_text = request.prompt or ""
    agent_prompt = _agent_prompt_prefix(request)
    inject_persona = bool(agent_prompt and request.session_manager is not None)
    if inject_persona:
        prompt_text = f"{agent_prompt}\n\n{prompt_text}" if prompt_text else agent_prompt
    return ProviderSpawnPlan(
        command=cmd,
        env=env,
        launch=launch,
        auth_cli="codex",
        child_session_id=gobby_session_id,
        agent_run_id=spawn_context.agent_run_id,
        title=f"gobby-codex-d{request.agent_depth}",
        codex_prompt=prompt_text,
        inject_persona=inject_persona,
    )


async def prepare_droid_spawn(request: SpawnRequest) -> ProviderSpawnPlan | SpawnResult:
    if validation_error := _session_manager_validation_error(request, "Droid"):
        return validation_error
    spawn_context = request.prepared_spawn
    gobby_session_id = spawn_context.session_id
    if preflight_error := await _prepare_managed_code_index(request, spawn_context):
        return preflight_error
    env = spawn_context.env_vars.copy()
    _apply_extra_env(env, request)
    if request.api_token:
        env["FACTORY_API_KEY"] = request.api_token
    if request.api_base:
        env["FACTORY_API_BASE_URL"] = request.api_base
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id
    sandbox_result = await _prepare_provider_sandbox(request, spawn_context, "droid", env)
    if isinstance(sandbox_result, SpawnResult):
        return sandbox_result
    launch = sandbox_result
    cmd, _cmd_env = build_cli_command(
        cli="droid",
        prompt=request.prompt,
        auto_approve=True,
        working_directory=request.cwd,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
        sandbox_args=launch.provider_args or None,
    )
    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=launch.provider_args,
        sandbox_env=launch.provider_env,
        env=env,
        sandbox_launch=launch,
    )
    pre_approve_directory("droid", request.cwd)
    return ProviderSpawnPlan(
        command=cmd,
        env=env,
        launch=launch,
        auth_cli="droid",
        child_session_id=gobby_session_id,
        agent_run_id=spawn_context.agent_run_id,
        title=f"gobby-droid-d{request.agent_depth}",
    )


async def prepare_agy_spawn(request: SpawnRequest) -> ProviderSpawnPlan | SpawnResult:
    if validation_error := _session_manager_validation_error(request, "AGY"):
        return validation_error
    spawn_context = request.prepared_spawn
    gobby_session_id = spawn_context.session_id
    if preflight_error := await _prepare_managed_code_index(request, spawn_context):
        return preflight_error
    env = spawn_context.env_vars.copy()
    _apply_extra_env(env, request)
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id
    sandbox_result = await _prepare_provider_sandbox(request, spawn_context, "agy", env)
    if isinstance(sandbox_result, SpawnResult):
        return sandbox_result
    launch = sandbox_result
    sandbox_args = list(launch.provider_args)
    if launch.enforced and launch.backend == "srt" and "--sandbox=false" not in sandbox_args:
        sandbox_args.append("--sandbox=false")
    cmd, _cmd_env = build_cli_command(
        cli="agy",
        prompt=request.prompt,
        auto_approve=True,
        working_directory=request.cwd,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
        sandbox_args=sandbox_args or None,
    )
    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=sandbox_args,
        sandbox_env=launch.provider_env,
        env=env,
        sandbox_launch=launch,
    )
    pre_approve_directory("agy", request.cwd)
    if request.initial_variables and request.session_manager is not None:
        storage = getattr(request.session_manager, "_storage", None)
        db = getattr(storage, "db", None)
        if db is not None:
            from gobby.workflows.state_manager import SessionVariableManager

            SessionVariableManager(db).merge_variables(
                gobby_session_id,
                dict(request.initial_variables),
            )
    return ProviderSpawnPlan(
        command=cmd,
        env=env,
        launch=launch,
        auth_cli="agy",
        child_session_id=gobby_session_id,
        agent_run_id=spawn_context.agent_run_id,
        title=f"gobby-agy-d{request.agent_depth}",
    )
