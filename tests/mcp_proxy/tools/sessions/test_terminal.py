"""Tests for tmux-backed session MCP terminal tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._terminal import (
    _FORBIDDEN_SPEED_COMMANDS,
    _is_speed_command,
    _resolve_tmux_target,
    register_terminal_tools,
)
from gobby.sessions.handoff import HandoffAttemptState
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import write_project_marker
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

MACHINE_ID = "30000000-0000-4000-8000-000000000003"


class _TestRegistry(InternalToolRegistry):
    """Registry subclass with get_tool for testing."""

    def get_tool(self, name: str) -> Callable[..., Any] | None:
        tool = self._tools.get(name)
        return tool.func if tool else None


def _persistent_session(
    temp_db: HubDatabase,
    tmp_path: Path,
    *,
    project_name: str = "gobby",
    session_type: str = "terminal",
) -> tuple[SessionManager, str]:
    checkout = tmp_path / f"{project_name}-{uuid4().hex}"
    checkout.mkdir()
    project_id = str(uuid4())
    write_project_marker(checkout, project_id=project_id, name=project_name)
    project = LocalProjectManager(temp_db).create(
        name=project_name,
        repo_path=str(checkout),
        project_id=project_id,
    )
    LocalMachineManager(temp_db).upsert_seen(MACHINE_ID, TEST_USER_ID)
    manager = SessionManager(temp_db)
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        session_id = manager.register_session(
            external_id=f"terminal-{uuid4().hex}",
            machine_id=MACHINE_ID,
            source="codex",
            project_id=project.id,
            project_path=str(checkout),
            terminal_context={"tmux_pane": "%12"},
        )
    assert session_id
    if session_type != "terminal":
        temp_db.execute(
            "UPDATE sessions SET session_type = %s WHERE id = %s",
            (session_type, session_id),
        )
        manager = SessionManager(temp_db)
    return manager, session_id


def _feedback_registry(
    temp_db: HubDatabase,
    manager: SessionManager,
    *,
    survey: str = "gobby",
    web_chat_session_registry: Any | None = None,
) -> _TestRegistry:
    from gobby.mcp_proxy.tools.sessions._handoff import register_handoff_tools

    registry = _TestRegistry(name="test", description="test")
    register_handoff_tools(registry, manager)
    register_terminal_tools(
        registry,
        manager,
        temp_db,
        web_chat_session_registry=web_chat_session_registry,
        config_resolver=lambda: SimpleNamespace(session_feedback=SimpleNamespace(survey=survey)),
    )
    return registry


def _feedback_observation(**overrides: object) -> dict[str, object]:
    observation: dict[str, object] = {
        "source": "gobby-sessions:set_handoff",
        "kind": "friction",
        "evidence": "The handoff needed an extra retry.",
        "impact": "One extra round trip.",
        "frequency": "once",
    }
    observation.update(overrides)
    return observation


def _feedback_row_count(temp_db: HubDatabase, session_id: str) -> int:
    row = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM session_feedback WHERE session_id = %s",
        (session_id,),
    )
    assert row is not None
    return int(row["count"])


def _handoff_row_count(temp_db: HubDatabase, session_id: str) -> int:
    row = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM session_handoffs WHERE session_id = %s",
        (session_id,),
    )
    assert row is not None
    return int(row["count"])


class TestResolveTmuxTarget:
    """Tests for session-to-tmux target resolution."""

    def test_returns_error_when_session_missing(self) -> None:
        """Missing sessions should not return a stale default-server marker."""
        session_manager = MagicMock()
        session_manager.get.return_value = None

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        target, tmux_manager, error = _resolve_tmux_target(
            "missing-session",
            session_manager,
            agent_run_manager,
        )

        assert target is None
        assert tmux_manager is None
        assert error == "Session missing-session not found"

    def test_accepts_json_terminal_context(self) -> None:
        """Stored terminal_context may be raw JSON text."""
        session = MagicMock()
        session.terminal_context = '{"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}'

        session_manager = MagicMock()
        session_manager.get.return_value = session

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.manager_for_terminal_context"
        ) as mock_get_tmux_manager:
            target, tmux_manager, error = _resolve_tmux_target(
                "session-1",
                session_manager,
                agent_run_manager,
            )

        assert target == "%12"
        assert tmux_manager == mock_get_tmux_manager.return_value
        assert error is None
        mock_get_tmux_manager.assert_called_once_with(
            {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}
        )

    def test_accepts_mapping_terminal_context(self) -> None:
        """Stored terminal_context may already be a parsed mapping."""
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}

        session_manager = MagicMock()
        session_manager.get.return_value = session

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.manager_for_terminal_context"
        ) as mock_get_tmux_manager:
            target, tmux_manager, error = _resolve_tmux_target(
                "session-1",
                session_manager,
                agent_run_manager,
            )

        assert target == "%12"
        assert tmux_manager == mock_get_tmux_manager.return_value
        assert error is None
        mock_get_tmux_manager.assert_called_once_with(session.terminal_context)

    def test_reports_invalid_terminal_context(self) -> None:
        """Malformed stored terminal_context returns a useful diagnostic."""
        session = MagicMock()
        session.terminal_context = "{not json"

        session_manager = MagicMock()
        session_manager.get.return_value = session

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        target, tmux_manager, error = _resolve_tmux_target(
            "session-1",
            session_manager,
            agent_run_manager,
        )

        assert target is None
        assert tmux_manager is None
        assert error == (
            "Session session-1 has invalid terminal_context (str); expected object or JSON object"
        )

    def test_reports_terminal_context_without_tmux_target(self) -> None:
        """A parsed context without a tmux target should explain its keys."""
        session = MagicMock()
        session.terminal_context = {"terminal": "tmux"}

        session_manager = MagicMock()
        session_manager.get.return_value = session

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        target, tmux_manager, error = _resolve_tmux_target(
            "session-1",
            session_manager,
            agent_run_manager,
        )

        assert target is None
        assert tmux_manager is None
        assert error == (
            "Session session-1 terminal_context has no tmux_pane or tmux_session (keys: terminal)"
        )


class TestIsSpeedCommand:
    """Tests for the send_keys provider-speed payload matcher."""

    @pytest.mark.parametrize(
        "keys",
        [
            "/fast",
            "/fast\n",
            "/fast\n\n",
            "  /FAST  ",
            "\t/Fast\r\n",
            "/fast --now",
            "/fast on",
        ],
    )
    def test_matches_the_toggle_however_it_is_typed(self, keys: str) -> None:
        """Leading and trailing whitespace, case, and trailing arguments do not evade it."""
        assert _is_speed_command(keys) is True

    @pytest.mark.parametrize(
        "keys",
        [
            "",
            "   ",
            "\n",
            "/faster",
            "/fastfoo",
            "/fas",
            "fast",
            "not /fast",
            "echo /fast",
            "//fast",
            "/compact",
        ],
    )
    def test_leaves_every_other_payload_alone(self, keys: str) -> None:
        """Only an exact `/fast` first token matches; prefixes and later tokens do not."""
        assert _is_speed_command(keys) is False

    def test_the_forbidden_set_is_exactly_the_one_live_toggle(self) -> None:
        """`/fast` is the only in-session speed toggle across the six supported CLIs."""
        assert _FORBIDDEN_SPEED_COMMANDS == frozenset({"/fast"})


class TestRegisterTerminalTools:
    """Tests for terminal interaction tool registration."""

    @pytest.mark.parametrize("source", ["claude", "codex", "grok", "qwen", "droid"])
    def test_set_handoff_stages_before_terminal_delivery(self, source: str) -> None:
        registry = _TestRegistry(name="test", description="test")
        session = MagicMock(
            id="session-1",
            project_id="project-1",
            session_type="terminal",
            source=source,
            status="active",
            terminal_context={"tmux_pane": "%12"},
        )
        session_manager = MagicMock()
        session_manager.get.return_value = session
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
        pane = MagicMock(backend="tmux", target="%12")
        pane.snapshot = AsyncMock(return_value="ready")
        state = HandoffAttemptState(
            session_id="session-1",
            attempt_id="unused",
            handoff_record_id="handoff-1",
            prior_handoff_markdown=None,
            prior_markers={},
            missing_markers=frozenset(),
        )

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=MagicMock(),
        ):
            register_terminal_tools(registry, session_manager, MagicMock())
        set_handoff = registry.get_tool("set_handoff")
        assert set_handoff is not None

        with (
            session_context_for_test("session-1"),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal._resolve_pane_io",
                return_value=(pane, None),
            ),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal._interrupt_observer",
                return_value=(None, None),
            ),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.stage_handoff_attempt",
                return_value=state,
            ),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal._send_terminal_compaction_command",
                new_callable=AsyncMock,
            ) as send_command,
        ):
            result = asyncio.run(set_handoff(current_state="Ready.", next_steps=["Continue."]))

        assert result["success"] is True
        assert result["handoff_staged"] is True
        assert result["delivery_pending"] is True
        assert result["session_id"] == "session-1"
        assert result["clear_session"] is False
        send_command.assert_not_awaited()

    def test_set_handoff_clear_stages_before_terminal_delivery(self) -> None:
        registry = _TestRegistry(name="test", description="test")
        session = MagicMock(
            id="session-1",
            project_id="project-1",
            session_type="terminal",
            source="qwen",
            status="active",
            terminal_context={"tmux_pane": "%12"},
        )
        session_manager = MagicMock()
        session_manager.get.return_value = session
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None
        pane = MagicMock(backend="tmux", target="%12")
        pane.snapshot = AsyncMock(return_value="ready")

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())
        set_handoff = registry.get_tool("set_handoff")
        assert set_handoff is not None

        with (
            session_context_for_test("session-1"),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal_clear._authorize_send_keys_target",
                return_value=("session-1", None),
            ),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal_clear._resolve_pane_io",
                return_value=(pane, None),
            ),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal_clear._interrupt_observer",
                return_value=(None, None),
            ),
            patch("gobby.mcp_proxy.tools.sessions._terminal_clear.stage_clear_attempt"),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal_clear._send_terminal_compaction_command",
                new_callable=AsyncMock,
            ) as send_command,
        ):
            result = asyncio.run(
                set_handoff(
                    current_state="Ready.",
                    next_steps=["Continue."],
                    clear_session=True,
                )
            )

        assert result["success"] is True
        assert result["handoff_staged"] is True
        assert result["delivery_pending"] is True
        assert result["clear_session"] is True
        assert result["command"] == "/clear"
        send_command.assert_not_awaited()

    def test_send_keys_uses_tmux_manager_for_recorded_socket(self) -> None:
        """Interactive sessions should route through the manager for their recorded tmux server."""
        registry = _TestRegistry(name="test", description="test")

        session = MagicMock()
        session.id = "session-1"
        session.project_id = "project-1"
        session.agent_run_id = None
        session.terminal_context = {
            "tmux_pane": "%12",
            "tmux_socket_path": "/tmp/tmux-1000/gobby",
        }

        session_manager = MagicMock()
        session_manager.get.return_value = session
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        tmux_manager = MagicMock()
        tmux_manager.send_keys = AsyncMock(return_value=True)
        tmux_manager.dispatch_keys = tmux_manager.send_keys

        with (
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
                return_value=agent_run_manager,
            ),
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        send_keys_metadata = registry.get_tool_metadata("send_keys")
        assert send_keys_metadata is not None
        assert (
            "one or more trailing \\n characters produce exactly one Enter after the literal paste settles"
            in send_keys_metadata.description
        )
        assert (
            "use `gobby-agents:send_message` for direct cross-session agent communication"
            in send_keys_metadata.description
        )
        send_keys = send_keys_metadata.func
        assert send_keys is not None

        with (
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.manager_for_terminal_context",
                return_value=tmux_manager,
            ) as mock_get_tmux_manager,
            patch(
                "gobby.utils.session_context.get_current_session_id",
                return_value="session-1",
            ),
        ):
            result = asyncio.run(send_keys(session_id="session-1", keys="hello\n", literal=True))

        assert result == {"success": True}
        mock_get_tmux_manager.assert_called_once_with(session.terminal_context)
        tmux_manager.send_keys.assert_awaited_once_with("%12", "hello\n", literal=True)

    def test_send_keys_rejects_target_outside_caller_scope(self) -> None:
        """Cross-project sessions outside the caller's agent tree cannot receive keys."""
        registry = _TestRegistry(name="test", description="test")
        caller = MagicMock(id="caller-session", project_id="project-1", agent_run_id=None)
        target = MagicMock(id="target-session", project_id="project-2")

        session_manager = MagicMock()
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
        session_manager.get.side_effect = {
            "caller-session": caller,
            "target-session": target,
        }.get
        session_manager.is_ancestor.return_value = False

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        send_keys = registry.get_tool("send_keys")
        assert send_keys is not None

        with patch(
            "gobby.utils.session_context.get_current_session_id",
            return_value="caller-session",
        ):
            result = asyncio.run(send_keys(session_id="target-session", keys="hello"))

        assert result == {
            "success": False,
            "error": "send_keys target is outside the caller's project and agent tree",
            "error_code": "send_keys_target_forbidden",
            "caller_session_id": "caller-session",
            "target_session_id": "target-session",
        }
        agent_run_manager.get_by_session.assert_not_called()

    def test_send_keys_rejects_autonomous_agent_caller(self) -> None:
        """Autonomous agent sessions cannot inject keystrokes into any terminal."""
        registry = _TestRegistry(name="test", description="test")
        caller = MagicMock(
            id="caller-session",
            project_id="project-1",
            agent_run_id="agent-run-1",
        )

        session_manager = MagicMock()
        session_manager.resolve_session_reference.return_value = "caller-session"
        session_manager.get.return_value = caller

        agent_run_manager = MagicMock()
        tmux_manager = MagicMock()

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        send_keys = registry.get_tool("send_keys")
        assert send_keys is not None

        with (
            patch(
                "gobby.utils.session_context.get_current_session_id",
                return_value="caller-session",
            ),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.manager_for_terminal_context",
                return_value=tmux_manager,
            ) as mock_get_tmux_manager,
        ):
            result = asyncio.run(send_keys(session_id="target-session", keys="hello"))

        assert result == {
            "success": False,
            "error": "Autonomous agent sessions cannot use send_keys",
            "error_code": "send_keys_autonomous_agent_forbidden",
            "caller_session_id": "caller-session",
        }
        session_manager.resolve_session_reference.assert_called_once_with("caller-session")
        session_manager.get.assert_called_once_with("caller-session")
        agent_run_manager.get_by_session.assert_not_called()
        mock_get_tmux_manager.assert_not_called()

    @pytest.mark.parametrize("relationship", ["same_project", "caller_ancestor", "target_ancestor"])
    def test_send_keys_allows_in_scope_target(self, relationship: str) -> None:
        """Same-project sessions and either direction of an agent lineage can receive keys."""
        registry = _TestRegistry(name="test", description="test")
        caller = MagicMock(id="caller-session", project_id="project-1", agent_run_id=None)
        target_project = "project-1" if relationship == "same_project" else "project-2"
        target = MagicMock(
            id="target-session",
            project_id=target_project,
            terminal_context={"tmux_pane": "%12"},
        )

        session_manager = MagicMock()
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
        session_manager.get.side_effect = {
            "caller-session": caller,
            "target-session": target,
        }.get
        session_manager.is_ancestor.side_effect = lambda ancestor, descendant: (
            (relationship == "caller_ancestor" and ancestor == "caller-session")
            or (relationship == "target_ancestor" and ancestor == "target-session")
        )

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None
        tmux_manager = MagicMock()
        tmux_manager.send_keys = AsyncMock(return_value=True)
        tmux_manager.dispatch_keys = tmux_manager.send_keys

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        send_keys = registry.get_tool("send_keys")
        assert send_keys is not None

        with (
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.manager_for_terminal_context",
                return_value=tmux_manager,
            ),
            patch(
                "gobby.utils.session_context.get_current_session_id",
                return_value="caller-session",
            ),
        ):
            result = asyncio.run(send_keys(session_id="target-session", keys="hello"))

        assert result == {"success": True}
        tmux_manager.send_keys.assert_awaited_once_with("%12", "hello", literal=True)

    @staticmethod
    def _authorized_send_keys(
        tmux_manager: MagicMock,
    ) -> Callable[..., Any]:
        """Register send_keys with a caller that clears every authorization check."""
        registry = _TestRegistry(name="test", description="test")
        caller = MagicMock(id="caller-session", project_id="project-1", agent_run_id=None)
        target = MagicMock(
            id="target-session",
            project_id="project-1",
            terminal_context={"tmux_pane": "%12"},
        )

        session_manager = MagicMock()
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
        session_manager.get.side_effect = {
            "caller-session": caller,
            "target-session": target,
        }.get

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        send_keys = registry.get_tool("send_keys")
        assert send_keys is not None
        return send_keys

    def test_send_keys_gates_the_speed_toggle_before_any_delivery_path(self) -> None:
        """The `/fast` refusal fires after authorization and before either delivery path."""
        tmux_manager = MagicMock()
        tmux_manager.send_keys = AsyncMock(return_value=True)
        tmux_manager.dispatch_keys = tmux_manager.send_keys
        send_keys = self._authorized_send_keys(tmux_manager)

        with (
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.manager_for_terminal_context",
                return_value=tmux_manager,
            ) as mock_get_tmux_manager,
            patch(
                "gobby.utils.session_context.get_current_session_id",
                return_value="caller-session",
            ),
        ):
            refused = asyncio.run(send_keys(session_id="target-session", keys="/fast\n"))
            delivered = asyncio.run(send_keys(session_id="target-session", keys="/faster\n"))

        assert refused == {
            "success": False,
            "error": "send_keys cannot toggle provider speed mode; ask the user to run it",
            "error_code": "send_keys_speed_command_forbidden",
        }
        assert delivered == {"success": True}
        # The refusal never resolved a pane, so neither the write-coordinator nor the
        # tmux fallback branch could have run; the nearby `/faster` proves the same
        # setup does deliver.
        mock_get_tmux_manager.assert_called_once_with({"tmux_pane": "%12"})
        tmux_manager.send_keys.assert_awaited_once_with("%12", "/faster\n", literal=True)

    def test_capture_output_uses_tmux_when_pane_exists(self) -> None:
        """capture_output reads the live pane when a tmux target is available."""
        registry = _TestRegistry(name="test", description="test")
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%12"}

        session_manager = MagicMock()
        session_manager.get.return_value = session
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None
        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value="live output")
        tmux_manager.snapshot_lines = tmux_manager.capture_pane

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        capture_metadata = registry.get_tool_metadata("capture_output")
        assert capture_metadata is not None
        assert "one-shot diagnostic snapshot" in capture_metadata.description
        assert "gobby-agents:wait_for_output" in capture_metadata.description
        assert "instead of repeated capture_output calls" in capture_metadata.description
        capture_output = capture_metadata.func
        assert capture_output is not None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.manager_for_terminal_context",
            return_value=tmux_manager,
        ):
            result = asyncio.run(capture_output(session_id="session-1", lines=20))

        assert result == {"success": True, "output": "live output", "via": "tmux"}
        tmux_manager.capture_pane.assert_awaited_once_with("%12", 20)

    def test_capture_output_falls_back_to_transcript_tail(self, tmp_path) -> None:
        """When no tmux target exists, capture_output returns a transcript tail."""
        registry = _TestRegistry(name="test", description="test")
        transcript = tmp_path / "codex.jsonl"
        transcript.write_text("one\n-two\nthree\n", encoding="utf-8")

        session = MagicMock()
        session.terminal_context = {"parent_pid": 12345}
        session.transcript_path = str(transcript)

        session_manager = MagicMock()
        session_manager.get.return_value = session
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        capture_output = registry.get_tool("capture_output")
        assert capture_output is not None

        result = asyncio.run(capture_output(session_id="session-1", lines=2))

        assert result["success"] is True
        assert result["via"] == "transcript"
        assert result["output"] == "-two\nthree"
        assert "No live tmux pane" in result["note"]

    def test_capture_output_reports_no_pane_or_transcript(self) -> None:
        """Missing tmux target plus missing transcript returns structured failure."""
        registry = _TestRegistry(name="test", description="test")
        session = MagicMock()
        session.terminal_context = {"parent_pid": 12345}
        session.transcript_path = None

        session_manager = MagicMock()
        session_manager.get.return_value = session
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        capture_output = registry.get_tool("capture_output")
        assert capture_output is not None

        result = asyncio.run(capture_output(session_id="session-1", lines=20))

        assert result["success"] is False
        assert result["error_code"] == "no_live_pane_or_transcript"
        assert result["transcript_error"] == "missing_transcript_path"


class TestSetHandoffFeedback:
    """Separate feedback submission is a prerequisite, never human review."""

    @pytest.mark.parametrize("clear_session", [False, True])
    def test_feedback_required_returns_without_staging(
        self, temp_db: HubDatabase, tmp_path: Path, clear_session: bool
    ) -> None:
        manager, session_id = _persistent_session(temp_db, tmp_path)
        set_handoff = _feedback_registry(temp_db, manager).get_tool("set_handoff")
        assert set_handoff is not None
        with session_context_for_test(session_id):
            result = asyncio.run(
                set_handoff(
                    current_state="Ready", next_steps=["Continue"], clear_session=clear_session
                )
            )
        assert result["error_code"] == "feedback_required"
        assert "gobby-sessions:feedback" in result["error"]
        assert _handoff_row_count(temp_db, session_id) == 0
        assert _feedback_row_count(temp_db, session_id) == 0

    @pytest.mark.parametrize("empty", [False, True])
    def test_separate_submission_allows_handoff_without_repeating_feedback(
        self, temp_db: HubDatabase, tmp_path: Path, empty: bool
    ) -> None:
        manager, session_id = _persistent_session(temp_db, tmp_path)
        registry = _feedback_registry(temp_db, manager)
        feedback = registry.get_tool("feedback")
        set_handoff = registry.get_tool("set_handoff")
        assert feedback is not None and set_handoff is not None
        pane = MagicMock(backend="tmux", target="%12")
        pane.snapshot = AsyncMock(return_value="ready")
        with (
            session_context_for_test(session_id),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal._resolve_pane_io",
                return_value=(pane, None),
            ),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal._interrupt_observer",
                return_value=(None, None),
            ),
        ):
            submitted = feedback(observations=[] if empty else [_feedback_observation()])
            assert submitted["success"] is True
            assert _handoff_row_count(temp_db, session_id) == 0
            result = asyncio.run(set_handoff(current_state="Ready", next_steps=["Continue"]))
        assert result["handoff_staged"] is True
        assert result["feedback_submitted"] is True
        assert _feedback_row_count(temp_db, session_id) == (0 if empty else 1)
        variables = SessionVariableManager(temp_db).get_variables(session_id)
        assert variables["_gobby_feedback_epoch_submitted"] is True
        assert "_gobby_feedback_epoch_reviewed" not in variables
        rows = temp_db.fetchall(
            "SELECT reviewed FROM session_feedback WHERE session_id = %s", (session_id,)
        )
        assert [row["reviewed"] for row in rows] == ([] if empty else [False])

    def test_submission_ack_failure_rolls_back_feedback(
        self, temp_db: HubDatabase, tmp_path: Path
    ) -> None:
        manager, session_id = _persistent_session(temp_db, tmp_path)
        feedback = _feedback_registry(temp_db, manager).get_tool("feedback")
        assert feedback is not None
        with (
            session_context_for_test(session_id),
            patch(
                "gobby.mcp_proxy.tools.sessions._handoff.SessionVariableManager.merge_variables",
                side_effect=RuntimeError("ack failed"),
            ),
            pytest.raises(RuntimeError, match="ack failed"),
        ):
            feedback(observations=[_feedback_observation()])
        assert _feedback_row_count(temp_db, session_id) == 0
        assert (
            not SessionVariableManager(temp_db)
            .get_variables(session_id)
            .get("_gobby_feedback_epoch_submitted")
        )

    def test_invalid_feedback_does_not_satisfy_handoff_gate(
        self, temp_db: HubDatabase, tmp_path: Path
    ) -> None:
        manager, session_id = _persistent_session(temp_db, tmp_path)
        registry = _feedback_registry(temp_db, manager)
        feedback, set_handoff = registry.get_tool("feedback"), registry.get_tool("set_handoff")
        assert feedback is not None and set_handoff is not None
        with session_context_for_test(session_id):
            invalid = feedback(observations=[_feedback_observation(source="close_task")])
            result = asyncio.run(set_handoff(current_state="Ready", next_steps=["Continue"]))
        assert invalid["error_code"] == "invalid_feedback"
        assert result["error_code"] == "feedback_required"
        assert (
            not SessionVariableManager(temp_db)
            .get_variables(session_id)
            .get("_gobby_feedback_epoch_submitted")
        )
        assert _feedback_row_count(temp_db, session_id) == 0
        assert _handoff_row_count(temp_db, session_id) == 0

    @pytest.mark.parametrize("clear_session", [False, True])
    def test_oversize_rejection_has_no_side_effects(
        self, temp_db: HubDatabase, tmp_path: Path, clear_session: bool
    ) -> None:
        manager, session_id = _persistent_session(temp_db, tmp_path)
        set_handoff = _feedback_registry(temp_db, manager).get_tool("set_handoff")
        assert set_handoff is not None
        before = SessionVariableManager(temp_db).get_variables(session_id)
        with session_context_for_test(session_id):
            result = asyncio.run(
                set_handoff(
                    current_state="x" * 10_001, next_steps=["Continue"], clear_session=clear_session
                )
            )
        assert result["error_code"] == "invalid_handoff"
        assert "Shorten the handoff and retry" in result["error"]
        assert _handoff_row_count(temp_db, session_id) == 0
        assert _feedback_row_count(temp_db, session_id) == 0
        assert SessionVariableManager(temp_db).get_variables(session_id) == before

    @pytest.mark.parametrize(("project_name", "survey"), [("gobby", "off"), ("other", "gobby")])
    def test_inactive_survey_allows_handoff(
        self, temp_db: HubDatabase, tmp_path: Path, project_name: str, survey: str
    ) -> None:
        manager, session_id = _persistent_session(temp_db, tmp_path, project_name=project_name)
        set_handoff = _feedback_registry(temp_db, manager, survey=survey).get_tool("set_handoff")
        assert set_handoff is not None
        pane = MagicMock(backend="tmux", target="%12")
        pane.snapshot = AsyncMock(return_value="ready")
        with (
            session_context_for_test(session_id),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal._resolve_pane_io",
                return_value=(pane, None),
            ),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal._interrupt_observer",
                return_value=(None, None),
            ),
        ):
            result = asyncio.run(set_handoff(current_state="Ready", next_steps=["Continue"]))
        assert result["handoff_staged"] is True
        assert result["feedback_submitted"] is False
