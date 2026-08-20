"""Tests for the clear_self MCP tool on gobby-sessions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._terminal import register_terminal_tools
from gobby.sessions.clear_continuation import CLEAR_ATTEMPT_VARIABLE
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-00000000000f"
PRIOR_SUMMARY = "prior summary before staging"
HANDOFF = "Continue epic #20539: the staged clear handoff is the work context."
TERMINAL = {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux-clear-self"}


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _variables(db: HubDatabase, session_id: str) -> dict[str, Any]:
    row = db.fetchone(
        "SELECT variables FROM session_variables WHERE session_id = %s",
        (session_id,),
    )
    if row is None:
        return {}
    raw = row["variables"]
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _summary(db: HubDatabase, session_id: str) -> str | None:
    row = db.fetchone(
        "SELECT summary_markdown FROM sessions WHERE id = %s",
        (session_id,),
    )
    assert row is not None
    value = row["summary_markdown"]
    if value is None:
        return None
    assert isinstance(value, str)
    return value


def _status(db: HubDatabase, session_id: str) -> str:
    row = db.fetchone("SELECT status FROM sessions WHERE id = %s", (session_id,))
    assert row is not None
    return str(row["status"])


def _seed_terminal_session(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    *,
    source: str = "claude",
) -> str:
    session_id = session_manager.register_session(
        external_id=f"clear-self-{uuid4()}",
        machine_id=LOCAL_MACHINE_ID,
        source=source,
        project_id=sample_project["id"],
        terminal_context=TERMINAL,
    )
    assert session_id
    updated = session_manager.update_summary(session_id, summary_markdown=PRIOR_SUMMARY)
    assert updated is not None
    return session_id


def _register_clear_self(
    session_manager: SessionManager | MagicMock,
    db: HubDatabase | MagicMock,
    *,
    agent_run: Any = None,
    send_keys_returns: bool = True,
) -> tuple[InternalToolRegistry, MagicMock]:
    registry = InternalToolRegistry(name="gobby-sessions", description="test")
    agent_run_manager = MagicMock()
    agent_run_manager.get_by_session.return_value = agent_run
    tmux_manager = MagicMock()
    tmux_manager.send_keys = AsyncMock(return_value=send_keys_returns)
    tmux_manager.capture_pane = AsyncMock(return_value="")
    with patch(
        "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
        return_value=agent_run_manager,
    ):
        register_terminal_tools(registry, session_manager, db)
    return registry, tmux_manager


def _call_clear_self(
    registry: InternalToolRegistry,
    tmux_manager: MagicMock,
    session_id: str,
    **kwargs: Any,
) -> Any:
    clear_self = registry.get_tool("clear_self")
    assert clear_self is not None
    cursor = MagicMock()
    cursor.saw_fresh_turn_aborted.return_value = True
    with (
        session_context_for_test(session_id),
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal.get_tmux_manager_for_context",
            return_value=tmux_manager,
        ),
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal._CODEX_INTERRUPT_SETTLE_SECONDS",
            0,
        ),
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal._DEFAULT_INTERRUPT_SETTLE_SECONDS",
            0,
        ),
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal._COMPACTION_REJECTION_SETTLE_SECONDS",
            0,
        ),
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal_clear.CodexRolloutCursor.at_eof",
            return_value=cursor,
        ),
    ):
        return asyncio.run(clear_self(**kwargs))


class TestClearSelfRegistration:
    def test_schema_requires_nonempty_handoff(self) -> None:
        registry, _tmux = _register_clear_self(MagicMock(), MagicMock())

        tool = registry.get_tool("clear_self")
        schema = registry.get_schema("clear_self")

        assert tool is not None
        assert schema is not None
        input_schema = schema["inputSchema"]
        assert "handoff" in input_schema["properties"]
        assert "handoff" in input_schema.get("required", [])
        assert "session_id" not in input_schema["properties"]

    @pytest.mark.parametrize("handoff", ["", "   ", "\n\t"])
    def test_empty_or_whitespace_handoff_is_error(self, handoff: str) -> None:
        registry, tmux = _register_clear_self(MagicMock(), MagicMock())

        result = _call_clear_self(registry, tmux, "s1", handoff=handoff)

        assert result["success"] is False
        assert "handoff" in str(result.get("error", "")).lower()
        tmux.send_keys.assert_not_awaited()


class TestClearSelfStaging:
    def test_handoff_and_marker_are_stored_before_clear_is_sent(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        session_id = _seed_terminal_session(session_manager, sample_project)
        registry, tmux = _register_clear_self(session_manager, temp_db)
        observed: dict[str, Any] = {}

        async def send_keys(_target: str, keys: str, *, literal: bool) -> bool:
            _ = literal
            if keys == "/clear\n":
                observed["summary"] = _summary(temp_db, session_id)
                observed["status"] = _status(temp_db, session_id)
                observed["marker"] = _variables(temp_db, session_id).get(CLEAR_ATTEMPT_VARIABLE)
            return True

        tmux.send_keys = AsyncMock(side_effect=send_keys)

        result = _call_clear_self(registry, tmux, session_id, handoff=HANDOFF)

        assert result["success"] is True
        assert result["session_id"] == session_id
        assert result["handoff_staged"] is True
        assert result["command_sent"] is True
        assert isinstance(result["attempt_id"], str) and result["attempt_id"]
        assert observed["summary"] == HANDOFF
        assert observed["status"] != "handoff_ready"
        marker = observed["marker"]
        assert isinstance(marker, dict)
        assert marker["attempt_id"] == result["attempt_id"]
        assert marker["consumed_by"] is None
        assert _summary(temp_db, session_id) == HANDOFF

    def test_storage_failure_never_sends_clear(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        session_id = _seed_terminal_session(session_manager, sample_project)
        registry, tmux = _register_clear_self(session_manager, temp_db)

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal_clear.stage_clear_attempt",
            side_effect=RuntimeError("session_variables unavailable"),
        ):
            result = _call_clear_self(registry, tmux, session_id, handoff=HANDOFF)

        assert result["success"] is False
        tmux.send_keys.assert_not_awaited()
        assert CLEAR_ATTEMPT_VARIABLE not in _variables(temp_db, session_id)
        assert _summary(temp_db, session_id) == PRIOR_SUMMARY

    def test_send_failure_after_staging_restores_prior_summary(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        session_id = _seed_terminal_session(session_manager, sample_project)
        registry, tmux = _register_clear_self(session_manager, temp_db, send_keys_returns=False)

        result = _call_clear_self(registry, tmux, session_id, handoff=HANDOFF)

        assert result["success"] is False
        assert CLEAR_ATTEMPT_VARIABLE not in _variables(temp_db, session_id)
        assert _summary(temp_db, session_id) == PRIOR_SUMMARY
        assert _status(temp_db, session_id) != "handoff_ready"


class TestClearSelfDelivery:
    def test_claude_sends_slash_clear_via_compaction_sender(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        session_id = _seed_terminal_session(session_manager, sample_project, source="claude")
        registry, tmux = _register_clear_self(session_manager, temp_db)

        result = _call_clear_self(registry, tmux, session_id, handoff=HANDOFF)

        assert result["success"] is True
        assert result["command_sent"] is True
        assert tmux.send_keys.await_args_list == [
            call("%12", "Escape", literal=False),
            call("%12", "/clear\n", literal=True),
        ]

    def test_codex_interrupts_then_sends_slash_clear(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        session_id = _seed_terminal_session(session_manager, sample_project, source="codex")
        registry, tmux = _register_clear_self(session_manager, temp_db)

        result = _call_clear_self(registry, tmux, session_id, handoff=HANDOFF)

        assert result["success"] is True
        assert result["command_sent"] is True
        assert tmux.send_keys.await_args_list == [
            call("%12", "C-c", literal=False),
            call("%12", "/clear\n", literal=True),
        ]


class TestClearSelfRejections:
    def test_agent_run_session_is_rejected_without_staging(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        session_id = _seed_terminal_session(session_manager, sample_project)
        registry, tmux = _register_clear_self(session_manager, temp_db, agent_run=MagicMock())

        result = _call_clear_self(registry, tmux, session_id, handoff=HANDOFF)

        assert result["success"] is False
        assert "agent" in str(result.get("error", "")).lower()
        tmux.send_keys.assert_not_awaited()
        assert CLEAR_ATTEMPT_VARIABLE not in _variables(temp_db, session_id)
        assert _summary(temp_db, session_id) == PRIOR_SUMMARY

    def test_web_chat_is_not_yet_supported(self) -> None:
        session = MagicMock()
        session.session_type = "web_chat"
        session.source = "claude"
        session.status = "active"
        session.terminal_context = {}
        session_manager = MagicMock()
        session_manager.get.return_value = session
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
        registry, tmux = _register_clear_self(session_manager, MagicMock())

        result = _call_clear_self(registry, tmux, "web-1", handoff=HANDOFF)

        assert result["success"] is False
        assert "web chat" in str(result.get("error", "")).lower()
        tmux.send_keys.assert_not_awaited()
