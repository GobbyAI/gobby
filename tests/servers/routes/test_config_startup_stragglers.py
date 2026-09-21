from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock, PropertyMock

import pytest
from fastapi import HTTPException

from gobby.config.runtime import ConfigRuntime
from gobby.servers.routes.attention import _run_tmux_payload
from gobby.storage.attention import AttentionRosterRow


def _starting_server() -> MagicMock:
    server = MagicMock()
    runtime = MagicMock(spec=ConfigRuntime)
    type(runtime).snapshot = PropertyMock(side_effect=RuntimeError("runtime starting"))
    server.services.config_runtime = runtime
    return server


def test_attention_roster_tmux_payload_startup_returns_retryable_503() -> None:
    server = _starting_server()
    run = AttentionRosterRow(
        kind="run",
        source_id="agent-run",
        session_id=None,
        lifecycle_status="running",
        task_id=None,
        task_ref=None,
        task_stage=None,
        provider="codex",
        model=None,
        pid=123,
        updated_at=None,
        terminal_context={},
        terminal_id="agent",
        terminal=None,
    )

    with pytest.raises(HTTPException) as raised:
        _run_tmux_payload(server, run)

    detail = cast(dict[str, object], raised.value.detail)
    assert raised.value.status_code == 503
    assert detail["retryable"] is True
