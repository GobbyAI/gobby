from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from gobby.servers.websocket.handlers.session_observe_support import (
    _can_proxy_attach_session,
    _read_session_variables,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX


@pytest.mark.parametrize("status", ["active", "paused", "awaiting_handoff"])
def test_eligible_tmux_session_can_proxy_attach(status: str) -> None:
    session = SimpleNamespace(
        session_type="terminal",
        status=status,
        terminal_context={"tmux_pane": "%1"},
    )

    assert _can_proxy_attach_session(session) is True


@pytest.mark.parametrize("status", ["expired", "deleted"])
def test_inactive_session_cannot_proxy_attach_even_with_explicit_flag(status: str) -> None:
    session = SimpleNamespace(
        session_type="terminal",
        status=status,
        terminal_context={"tmux_pane": "%1"},
        can_proxy_attach=True,
    )

    assert _can_proxy_attach_session(session) is False


@pytest.mark.integration
def test_observe_reader_does_not_decode_unrelated_variables(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = f"{TEST_MACHINE_ID_PREFIX}000000000001"
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", machine_id)
    session = SessionManager(temp_db).register(
        external_id=str(uuid4()),
        machine_id=machine_id,
        source="claude",
        project_id=sample_project["id"],
        session_type="terminal",
        terminal_context={"tmux_pane": "%1"},
    )
    expected = {
        "chat_mode": "bypass",
        "mode_level": 1,
        "reasoning_effort": "high",
        "_effective_reasoning_effort": "medium",
        "_requested_reasoning_effort": "low",
        "model": "observed-model",
        "model_id": "observed-model-id",
        "modelId": "observed-model-alias",
        "context_window": 200000,
        "model_context_window": 128000,
        "modelContextWindow": 64000,
        "_local_context_route": {"model_id": "local-model"},
        "_local_context_observation": {"effective_limit": 32768},
    }
    unrelated = "x" * (4 * 1024 * 1024)
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(session.id, {**expected, "unrelated": unrelated})
    decode_sizes: list[int] = []
    decode = json.loads

    def observe_decode(payload: str | bytes | bytearray, **kwargs: Any) -> Any:
        decode_sizes.append(len(payload))
        return decode(payload, **kwargs)

    monkeypatch.setattr(json, "loads", observe_decode)
    observed = _read_session_variables(temp_db, session.id)
    assert decode_sizes
    assert max(decode_sizes) < 4096
    assert observed == expected
    assert variables.get_variables(session.id)["unrelated"] == unrelated
