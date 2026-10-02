"""A resumed run never relaunches outside managed SRT (placed-agent-launch 1.8.3)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents import resume_executor
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
