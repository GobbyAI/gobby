import logging
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn import PreparedSpawn
from gobby.agents.spawn_executor_providers import _prepare_provider_sandbox, prepare_codex_spawn
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
        terminal_backend="tmux",
    )
    return request, spawn_context, run_manager


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
    assert (
        'plugins."unified-computer-use@openai-bundled".enabled=false' in overrides
    ) is plugin_disabled
    assert ("mcp_servers.node_repl.enabled=false" in overrides) is plugin_disabled


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
