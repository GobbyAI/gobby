"""A resumed run never relaunches outside managed SRT (placed-agent-launch 1.8.3)."""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents import resume_executor
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.srt_runtime import SrtRuntimeError
from tests.agents.test_resume_executor import (
    _SUCCESSOR_ID,
    _original_run,
    _patch_common,
    _resume_metadata,
    _runner,
)

pytestmark = pytest.mark.unit

_ABSENT = object()


@pytest.mark.parametrize(
    "sandbox_config",
    [
        pytest.param(_ABSENT, id="no-snapshot"),
        pytest.param({"enabled": False, "backend": "srt"}, id="disabled"),
        pytest.param({"enabled": True, "backend": "provider-native"}, id="provider-native"),
    ],
)
async def test_resume_refuses_unsandboxed_config(
    monkeypatch: pytest.MonkeyPatch, sandbox_config: Any
) -> None:
    storage = MagicMock()
    runner = _runner(storage=storage)
    finalize = AsyncMock()
    _patch_common(monkeypatch, spawner=MagicMock(), finalize=finalize)
    prepare_sandbox = AsyncMock()
    monkeypatch.setattr(resume_executor, "prepare_sandbox_launch", prepare_sandbox)
    metadata = _resume_metadata()
    if sandbox_config is _ABSENT:
        del metadata["sandbox_config"]
    else:
        metadata["sandbox_config"] = sandbox_config

    with patch("gobby.agents.sandbox_gate.verify_srt_installation") as verifier:
        result = await resume_executor.resume_agent_run(
            _original_run(),
            resume_metadata=metadata,
            runner=runner,
            session_manager=MagicMock(),
        )

    assert result.success is False
    assert result.error == "sandbox_required"
    finalize.assert_awaited_once()
    storage.cancel.assert_called_once_with(str(_SUCCESSOR_ID), terminal_reason="daemon_stop")
    verifier.assert_not_called()
    prepare_sandbox.assert_not_awaited()
    assert runner._test_runtime.create_calls == 0


def _resume_task(runner: Any) -> asyncio.Task[Any]:
    return asyncio.create_task(
        resume_executor.resume_agent_run(
            _original_run(),
            resume_metadata=_resume_metadata(),
            runner=runner,
            session_manager=MagicMock(),
        )
    )


async def _cancel_twice(task: asyncio.Task[Any]) -> None:
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)


async def test_cancel_during_sandbox_verification_parks_successor_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = MagicMock()
    runner = _runner(storage=storage)
    finalize = AsyncMock()
    _patch_common(monkeypatch, spawner=MagicMock(), finalize=finalize)
    entered, release = threading.Event(), threading.Event()

    def unresolved_verifier(_metadata: dict[str, Any]) -> SandboxConfig:
        entered.set()
        release.wait(5)
        return SandboxConfig(enabled=True, backend="srt")

    monkeypatch.setattr(resume_executor, "resolve_resume_sandbox", unresolved_verifier)
    task = _resume_task(runner)
    assert await asyncio.to_thread(entered.wait, 5)
    await _cancel_twice(task)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    finalize.assert_awaited_once()
    storage.cancel.assert_called_once_with(str(_SUCCESSOR_ID), terminal_reason="daemon_stop")
    assert runner._test_runtime.create_calls == 0


async def test_cancel_during_refusal_parking_still_parks_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = MagicMock()
    runner = _runner(storage=storage)
    parking, unblock = asyncio.Event(), asyncio.Event()

    async def held_finalize(*_args: Any, **_kwargs: Any) -> None:
        parking.set()
        await unblock.wait()

    finalize = AsyncMock(side_effect=held_finalize)
    _patch_common(monkeypatch, spawner=MagicMock(), finalize=finalize)
    task = _resume_task(runner)
    with patch(
        "gobby.agents.sandbox_gate.verify_srt_installation",
        side_effect=SrtRuntimeError("not installed"),
    ):
        await asyncio.wait_for(parking.wait(), 5)
        await _cancel_twice(task)
        unblock.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    finalize.assert_awaited_once()
    storage.cancel.assert_called_once_with(str(_SUCCESSOR_ID), terminal_reason="daemon_stop")
    assert runner._test_runtime.create_calls == 0
