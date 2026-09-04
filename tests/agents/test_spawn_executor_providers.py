import logging
from unittest.mock import MagicMock

import pytest

from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn import PreparedSpawn
from gobby.agents.spawn_executor_providers import _prepare_provider_sandbox
from gobby.agents.spawn_models import SpawnRequest, SpawnResult
from gobby.agents.srt_runtime import SrtRuntimeError
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
