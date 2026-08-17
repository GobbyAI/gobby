"""Cron shell failures must persist complete stdout."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.scheduler.executor import CronExecutor, CronShellError
from gobby.storage.cron_models import CronJob, CronRun


@pytest.mark.asyncio
async def test_execute_shell_raises_error_with_full_output() -> None:
    body = "z" * 3500 + "\n"
    process = MagicMock()
    process.returncode = 1
    process.communicate = AsyncMock(return_value=(body.encode(), b""))

    executor = CronExecutor(storage=MagicMock())
    job = SimpleNamespace(action_config={"command": "cmd", "args": []})

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "gobby.scheduler.executor.asyncio.create_subprocess_exec",
            AsyncMock(return_value=process),
        )
        with pytest.raises(CronShellError) as exc_info:
            await executor._execute_shell(cast(CronJob, job))

    assert exc_info.value.output == body
    assert body not in str(exc_info.value)
    assert "full output stored on cron run" in str(exc_info.value)
    assert "[truncated]" in str(exc_info.value)


@pytest.mark.asyncio
async def test_execute_persists_shell_output_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "z" * 3500
    storage = MagicMock()
    storage.update_run.return_value = SimpleNamespace(status="failed", output=body)
    executor = CronExecutor(storage=storage)
    job = SimpleNamespace(id="job-1", name="fail", action_type="shell", action_config={})
    run = SimpleNamespace(id="run-1")

    async def fail_shell(_job: CronJob) -> str:
        raise CronShellError("boom [truncated]\n tail", output=body)

    monkeypatch.setattr(executor, "_execute_shell", fail_shell)
    result = await executor.execute(cast(CronJob, job), cast(CronRun, run))

    stored = storage.update_run.call_args_list[-1]
    assert stored.kwargs["output"] == body
    assert stored.kwargs["error"] == "boom [truncated]\n tail"
    assert stored.kwargs["status"] == "failed"
    assert result.status == "failed"
