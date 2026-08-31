from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, PropertyMock

import pytest
from fastapi import HTTPException

from gobby.config.runtime import ConfigRuntime
from gobby.servers.routes.attention import _run_tmux_payload


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
