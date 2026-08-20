from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from fastapi import HTTPException

from gobby.config.runtime import ConfigRuntime
from gobby.servers.routes.attention import _resolve_attention_pane, _run_tmux_payload
from gobby.storage.attention import AttentionState
from tests.agents.terminal_fixtures import make_live_terminal, make_pending_terminal


def _starting_server() -> MagicMock:
    server = MagicMock()
    runtime = MagicMock(spec=ConfigRuntime)
    type(runtime).snapshot = PropertyMock(side_effect=RuntimeError("runtime starting"))
    server.services.config_runtime = runtime
    return server


def test_attention_roster_tmux_payload_startup_returns_retryable_503() -> None:
    server = _starting_server()
    run = SimpleNamespace(terminal_id="agent", pid=123)

    with pytest.raises(HTTPException) as raised:
        _run_tmux_payload(server, run)

    detail = cast(dict[str, object], raised.value.detail)
    assert raised.value.status_code == 503
    assert detail["retryable"] is True


@pytest.mark.asyncio
async def test_attention_pane_startup_returns_retryable_503() -> None:
    server = _starting_server()
    server.services.session_manager = None
    server.services.agent_runner.get_run = AsyncMock(
        return_value=SimpleNamespace(terminal_id="agent")
    )
    server.services.run_db = AsyncMock(return_value=SimpleNamespace(terminal_id="agent"))
    state = cast(AttentionState, SimpleNamespace(run_id="1", session_id=None))

    with pytest.raises(HTTPException) as raised:
        await _resolve_attention_pane(server, state)

    detail = cast(dict[str, object], raised.value.detail)
    assert raised.value.status_code == 503
    assert detail["retryable"] is True
