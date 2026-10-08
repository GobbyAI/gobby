import logging
import tomllib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents import spawn_executor_codex
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn import PreparedSpawn
from gobby.agents.spawn_executor_providers import (
    ProviderSpawnPlan,
    _prepare_provider_sandbox,
    prepare_codex_spawn,
)
from gobby.agents.spawn_models import SpawnRequest, SpawnResult
from gobby.agents.srt_runtime import SandboxLaunch, SrtRuntimeError
from tests.agents.prepared_spawn import prepared_spawn


def _sandbox_request() -> tuple[SpawnRequest, PreparedSpawn, MagicMock]:
    run_manager = MagicMock()
    spawn_context = prepared_spawn(
        session_id="child",
        agent_run_id="actual-run",
        env_vars={"GOBBY_SESSION_ID": "child"},
    )
    request = SpawnRequest(
        prompt="work",
        cwd="/workspace",
        provider="codex",
        session_id="parent",
        run_id="requested-run",
        parent_session_id="parent",
        project_id="project",
        run_manager=run_manager,
        sandbox_config=SandboxConfig(enabled=True, backend="srt"),
        prepared_spawn=spawn_context,
    )
    return request, spawn_context, run_manager


@pytest.mark.asyncio
async def test_codex_spawn_uses_sandbox_effective_cache_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, spawn_context, _ = _sandbox_request()
    cache_env = {
        "UV_CACHE_DIR": "/sandbox/cache/uv",
        "CARGO_HOME": "/sandbox/shared/cargo-home",
        "CARGO_TARGET_DIR": "/sandbox/checkout/cargo-target",
    }
    spawn_context.env_vars.update({name: f"/operator/{name}" for name in cache_env})
    request = replace(request, project_path="/main/repo", session_manager=MagicMock())
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_managed_code_index",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_provider_sandbox",
        AsyncMock(return_value=SandboxLaunch(backend="srt", enforced=True, provider_env=cache_env)),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._record_resume_launch_details", MagicMock()
    )
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.pre_approve_directory", MagicMock())
    build_command = MagicMock(return_value=(["codex"], {}))
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.build_cli_command", build_command)

    result = await prepare_codex_spawn(request)

    assert isinstance(result, ProviderSpawnPlan)
    config = tomllib.loads("\n".join(build_command.call_args.kwargs["config_overrides"]))
    for name, path in cache_env.items():
        assert config["shell_environment_policy"]["set"][name] == path
        assert config["mcp_servers"]["gobby"]["env"][name] == path


@pytest.mark.parametrize(
    ("agent_name", "plugin_disabled"),
    [("task-close-reviewer", True), ("backend-developer", False)],
)
@pytest.mark.asyncio
async def test_codex_close_reviewer_launch_hides_execution_wrappers(
    monkeypatch: pytest.MonkeyPatch, agent_name: str, plugin_disabled: bool
) -> None:
    request, _, _ = _sandbox_request()
    request = replace(
        request,
        agent_name=agent_name,
        project_path="/main/repo",
        session_manager=MagicMock(),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_managed_code_index",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_provider_sandbox",
        AsyncMock(return_value=SandboxLaunch(backend="provider-native", enforced=False)),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._record_resume_launch_details", MagicMock()
    )
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.pre_approve_directory", MagicMock())
    build_command = MagicMock(return_value=(["codex"], {}))
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.build_cli_command", build_command)

    await prepare_codex_spawn(request)

    overrides = build_command.call_args.kwargs["config_overrides"]
    assert "mcp_servers.gobby.required=true" in overrides
    assert ("features.plugins=false" in overrides) is plugin_disabled
    assert ("features.remote_plugin=false" in overrides) is plugin_disabled
    assert (
        'plugins."unified-computer-use@openai-bundled".enabled=false' in overrides
    ) is plugin_disabled
    assert ("mcp_servers.node_repl.enabled=false" in overrides) is plugin_disabled


@pytest.mark.asyncio
async def test_srt_codex_close_reviewer_launches_headless_with_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _, _ = _sandbox_request()
    request = replace(
        request,
        agent_name="task-close-reviewer",
        prompt="Review task close",
        session_manager=MagicMock(),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_managed_code_index",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_provider_sandbox",
        AsyncMock(return_value=SandboxLaunch(backend="srt", enforced=True)),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._record_resume_launch_details", MagicMock()
    )
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.pre_approve_directory", MagicMock())
    build_command = MagicMock(return_value=(["codex", "exec"], {}))
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.build_cli_command", build_command)

    plan = await prepare_codex_spawn(request)

    assert not isinstance(plan, SpawnResult)
    assert plan.command == ["codex", "exec"]
    assert plan.codex_prompt is None
    assert build_command.call_args.kwargs["mode"] == "headless"
    assert build_command.call_args.kwargs["prompt"] == "Review task close"
    assert 'sandbox_mode="danger-full-access"' in build_command.call_args.kwargs["config_overrides"]
    assert build_command.call_args.kwargs["external_sandbox_enforced"] is True


@pytest.mark.asyncio
async def test_headless_codex_persona_is_marked_before_runtime_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _, _ = _sandbox_request()
    request = replace(request, session_manager=MagicMock())
    plan = ProviderSpawnPlan(
        command=["codex", "exec", "Review"],
        env={},
        launch=SandboxLaunch(backend="srt", enforced=True),
        auth_cli="codex",
        child_session_id="child",
        agent_run_id="actual-run",
        codex_prompt=None,
        inject_persona=True,
    )
    events: list[tuple[str, object]] = []

    def mark_persona(child_session_id: str, values: dict[str, bool]) -> None:
        events.append(("mark", (child_session_id, values)))

    monkeypatch.setattr(
        "gobby.workflows.state_manager.SessionVariableManager",
        lambda _db: SimpleNamespace(merge_variables=mark_persona),
    )
    monkeypatch.setattr(spawn_executor_codex, "prepare_codex_spawn", AsyncMock(return_value=plan))
    result = SpawnResult(
        success=True, run_id="actual-run", child_session_id="child", status="running"
    )

    async def runtime_spawn(_request: SpawnRequest, _plan: ProviderSpawnPlan) -> SpawnResult:
        events.append(("spawn", (_request, _plan)))
        return result

    monkeypatch.setattr("gobby.agents.spawn_executor._runtime_spawn", runtime_spawn)

    spawned = await spawn_executor_codex._spawn_codex_terminal(request)
    assert spawned is result
    assert spawned.success is True
    assert events == [
        ("mark", ("child", {"_agent_context_injected": True})),
        ("spawn", (request, plan)),
    ]


@pytest.mark.parametrize(
    ("exception", "expected_detail"),
    [
        (TimeoutError(), "TimeoutError"),
        (
            SrtRuntimeError("managed SRT preflight timed out"),
            "SrtRuntimeError: managed SRT preflight timed out",
        ),
    ],
)
@pytest.mark.asyncio
async def test_sandbox_failure_names_exception_class(
    monkeypatch: pytest.MonkeyPatch,
    exception: Exception,
    expected_detail: str,
) -> None:
    request, spawn_context, run_manager = _sandbox_request()

    async def fail_sandbox_launch(**_kwargs: object) -> None:
        raise exception

    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers.prepare_sandbox_launch",
        fail_sandbox_launch,
    )

    result = await _prepare_provider_sandbox(request, spawn_context, "codex", {})

    assert isinstance(result, SpawnResult)
    expected_error = f"Sandbox startup failed closed for codex: {expected_detail}"
    assert result.error == expected_error
    run_manager.fail.assert_called_once_with("actual-run", expected_error)


@pytest.mark.asyncio
async def test_sandbox_failure_logs_traceback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request, spawn_context, _run_manager = _sandbox_request()
    exception = TimeoutError()

    async def fail_sandbox_launch(**_kwargs: object) -> None:
        raise exception

    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers.prepare_sandbox_launch",
        fail_sandbox_launch,
    )
    caplog.set_level(logging.WARNING, logger="gobby.agents.spawn_executor_providers")

    result = await _prepare_provider_sandbox(request, spawn_context, "codex", {})

    assert isinstance(result, SpawnResult)
    records = [
        record
        for record in caplog.records
        if record.message == "Sandbox startup failed closed for codex: TimeoutError"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None
    assert record.exc_info[1] is exception
