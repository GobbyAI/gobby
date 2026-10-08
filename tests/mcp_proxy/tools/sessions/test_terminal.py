"""Tests for managed session MCP terminal tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.hooks._normalization_tools import normalize_tool_fields
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.terminal_handoff_delivery import schedule_terminal_handoff_delivery
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import _terminal_clear
from gobby.mcp_proxy.tools.sessions._terminal import register_terminal_tools
from gobby.mcp_proxy.tools.sessions._terminal_send_keys import (
    _FORBIDDEN_SPEED_COMMANDS,
    _is_speed_command,
)
from gobby.sessions.handoff import (
    FAILED_HANDOFF_VARIABLE,
    HANDOFF_DELIVERY_FAILURES_VARIABLE,
    HANDOFF_DISPATCH_GATE_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    HandoffAttemptState,
    claim_staged_handoff_delivery,
    consume_pending_handoff,
)
from gobby.sessions.handoff_records import record_handoff_delivery
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.terminals.runtime import Delivered, TerminalWriteError
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
            register_terminal_tools(
                registry, session_manager, MagicMock(fetchone=MagicMock(return_value=None))
            )
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

    @pytest.mark.parametrize("clear_session", [False, True])
    @pytest.mark.parametrize(
        ("error_code", "reason"),
        [
            ("compact_unconfirmed", None),
            (
                None,
                "native key write failed (none): enter "
                "(session session-1 while submitting /compact)",
            ),
        ],
    )
    @pytest.mark.parametrize("readiness_unconfirmed", [False, True])
    def test_set_handoff_rejects_second_attempt_while_compact_unconfirmed(
        self,
        clear_session: bool,
        error_code: str | None,
        reason: str | None,
        readiness_unconfirmed: bool,
    ) -> None:
        registry = _TestRegistry(name="test", description="test")
        session = MagicMock(
            id="session-1",
            project_id="project-1",
            session_type="terminal",
            source="codex",
            status="active",
            terminal_context={"tmux_pane": "%12"},
        )
        session_manager = MagicMock()
        session_manager.get.return_value = session
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
        pane = MagicMock(backend="tmux", target="%12")
        pane.snapshot = AsyncMock(return_value="ready")
        attempt_id = "a" * 32

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=MagicMock(),
        ):
            register_terminal_tools(
                registry, session_manager, MagicMock(fetchone=MagicMock(return_value=None))
            )
        set_handoff = registry.get_tool("set_handoff")
        assert set_handoff is not None

        with (
            session_context_for_test("session-1"),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.SessionVariableManager"
            ) as variable_manager,
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal._resolve_pane_io",
                return_value=(pane, None),
            ),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal._interrupt_observer",
                return_value=(None, None),
            ),
            patch("gobby.mcp_proxy.tools.sessions._terminal.stage_handoff_attempt") as stage,
        ):
            gate = {
                "delivery_failed": True,
                "attempt_id": attempt_id,
                "clear_session": False,
            }
            if error_code is not None:
                gate["error_code"] = error_code
            if reason is not None:
                gate["reason"] = reason
            variables = {
                "context_compact_handoff_result": gate,
                "failed_handoff_attempt": {"attempt_id": attempt_id},
            }
            if readiness_unconfirmed:
                gate["readiness_unconfirmed"] = True
                variables.pop("failed_handoff_attempt")
                variables["set_handoff_pending"] = {"attempt_id": attempt_id}
            variable_manager.return_value.get_variables.return_value = variables
            result = asyncio.run(
                set_handoff(
                    current_state="New handoff",
                    next_steps=["Continue."],
                    clear_session=clear_session,
                )
            )

        assert result["error_code"] == "compact_unconfirmed"
        assert result["attempt_id"] == attempt_id
        assert result["handoff_staged"] is False
        stage.assert_not_called()

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
            register_terminal_tools(
                registry, session_manager, MagicMock(fetchone=MagicMock(return_value=None))
            )
        set_handoff = registry.get_tool("set_handoff")
        assert set_handoff is not None

        with (
            session_context_for_test("session-1"),
            patch.object(
                _terminal_clear,
                "_authorize_send_keys_target",
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

    def assert_send_keys_rejects_raw_tmux_context_after_native_lookup(self) -> None:
        """A pane hint alone cannot authorize a raw tmux write."""
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

        terminal_manager = MagicMock()
        terminal_manager.resolve_live_for_session.return_value = None
        write_coordinator = MagicMock()

        register_terminal_tools(
            registry,
            session_manager,
            MagicMock(fetchone=MagicMock(return_value=None)),
            terminal_manager=terminal_manager,
            write_coordinator=write_coordinator,
        )

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

        with patch(
            "gobby.utils.session_context.get_current_session_id",
            return_value="session-1",
        ):
            result = asyncio.run(send_keys(session_id="session-1", keys="hello\n", literal=True))

        assert result["success"] is False
        assert result["error_code"] == "terminal_target_unavailable"
        assert isinstance(result["idempotency_key"], str)
        terminal_manager.resolve_live_for_session.assert_called_once_with(session)
        write_coordinator.write.assert_not_called()

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

        register_terminal_tools(
            registry, session_manager, MagicMock(fetchone=MagicMock(return_value=None))
        )

        send_keys = registry.get_tool("send_keys")
        assert send_keys is not None

        with patch(
            "gobby.utils.session_context.get_current_session_id",
            return_value="caller-session",
        ):
            result = asyncio.run(send_keys(session_id="target-session", keys="hello"))

        assert result == {
            "success": False,
            "error": "Autonomous agent sessions cannot use send_keys",
            "error_code": "send_keys_autonomous_agent_forbidden",
            "caller_session_id": "caller-session",
            "idempotency_key": result["idempotency_key"],
        }
        session_manager.resolve_session_reference.assert_called_once_with("caller-session")
        session_manager.get.assert_called_once_with("caller-session")

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

        terminal_manager = MagicMock()
        terminal_manager.resolve_live_for_session.return_value = SimpleNamespace(
            id="terminal-1", backend="native"
        )
        write_coordinator = MagicMock()
        write_coordinator.write = AsyncMock(return_value=Delivered())
        register_terminal_tools(
            registry,
            session_manager,
            MagicMock(fetchone=MagicMock(return_value=None)),
            terminal_manager=terminal_manager,
            write_coordinator=write_coordinator,
        )

        send_keys = registry.get_tool("send_keys")
        assert send_keys is not None

        with patch(
            "gobby.utils.session_context.get_current_session_id",
            return_value="caller-session",
        ):
            result = asyncio.run(send_keys(session_id="target-session", keys="hello"))

        assert result["success"] is True
        assert isinstance(result["idempotency_key"], str)
        request = write_coordinator.write.await_args.args[0]
        assert request.terminal_id == "terminal-1"
        assert request.payload == "hello"

    @staticmethod
    def _authorized_send_keys() -> tuple[Callable[..., Any], MagicMock, MagicMock]:
        """Register send_keys with a caller that clears every authorization check."""
        registry = _TestRegistry(name="test", description="test")
        caller = MagicMock(id="caller-session", project_id="project-1", agent_run_id=None)
        target = MagicMock(
            id="target-session",
            project_id="project-1",
            terminal_context=None,
        )

        session_manager = MagicMock()
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
        session_manager.get.side_effect = {
            "caller-session": caller,
            "target-session": target,
        }.get

        terminal_manager = MagicMock()
        terminal_manager.resolve_live_for_session.return_value = SimpleNamespace(
            id="terminal-1", backend="native"
        )
        write_coordinator = MagicMock()
        write_coordinator.write = AsyncMock(return_value=Delivered())
        register_terminal_tools(
            registry,
            session_manager,
            MagicMock(fetchone=MagicMock(return_value=None)),
            terminal_manager=terminal_manager,
            write_coordinator=write_coordinator,
        )

        send_keys = registry.get_tool("send_keys")
        assert send_keys is not None
        return send_keys, terminal_manager, write_coordinator

    def test_send_keys_gates_the_speed_toggle_before_any_delivery_path(self) -> None:
        """The `/fast` refusal fires after authorization and before native delivery."""
        send_keys, terminal_manager, write_coordinator = self._authorized_send_keys()

        with (
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
            "idempotency_key": refused["idempotency_key"],
        }
        assert delivered["success"] is True
        assert isinstance(delivered["idempotency_key"], str)
        terminal_manager.resolve_live_for_session.assert_called_once()
        write_coordinator.write.assert_awaited_once()
        request = write_coordinator.write.await_args.args[0]
        assert request.payload == "/faster"
        assert request.submit is True

    def test_capture_output_does_not_probe_raw_tmux_context(self) -> None:
        """A pane hint without a managed row cannot trigger raw tmux capture."""
        registry = _TestRegistry(name="test", description="test")
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%12"}

        session_manager = MagicMock()
        session_manager.get.return_value = session
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(
                registry, session_manager, MagicMock(fetchone=MagicMock(return_value=None))
            )

        capture_metadata = registry.get_tool_metadata("capture_output")
        assert capture_metadata is not None
        assert "one-shot diagnostic snapshot" in capture_metadata.description
        assert "gobby-agents:wait_for_output" in capture_metadata.description
        assert "instead of repeated capture_output calls" in capture_metadata.description
        capture_output = capture_metadata.func
        assert capture_output is not None

        result = asyncio.run(capture_output(session_id="session-1", lines=20))

        assert result["success"] is False
        assert result["error_code"] == "no_live_pane_or_transcript"

    def test_capture_output_falls_back_to_transcript_tail(self, tmp_path: Path) -> None:
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
            register_terminal_tools(
                registry, session_manager, MagicMock(fetchone=MagicMock(return_value=None))
            )

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
            register_terminal_tools(
                registry, session_manager, MagicMock(fetchone=MagicMock(return_value=None))
            )

        capture_output = registry.get_tool("capture_output")
        assert capture_output is not None

        result = asyncio.run(capture_output(session_id="session-1", lines=20))

        assert result["success"] is False
        assert result["error_code"] == "no_live_pane_or_transcript"
        assert result["transcript_error"] == "missing_transcript_path"

    def test_capture_output_uses_unbound_gterm_named_by_context(self) -> None:
        """An unbound gterm row named by context is captured instead of the transcript."""
        terminal_id = "11111111-1111-4111-8111-111111111111"
        terminal = MagicMock(
            backend="native",
            id=terminal_id,
            state="live",
            project_id="proj-1",
            agent_run_id=None,
            session_id=None,
        )
        session = MagicMock(
            id="session-1",
            project_id="proj-1",
            terminal_context={"gobby_terminal_id": terminal_id, "tmux_pane": None},
            transcript_path=None,
        )
        session_manager = MagicMock()
        session_manager.get.return_value = session
        terminal_manager = MagicMock()
        terminal_manager.get_live_for_session.return_value = None
        terminal_manager.get.return_value = terminal
        runtime = MagicMock()
        runtime.snapshot = AsyncMock(return_value=SimpleNamespace(text="live pane"))
        runtime_registry = MagicMock()
        runtime_registry.resolve.return_value = runtime
        registry = _TestRegistry(name="test", description="test")

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=MagicMock(),
        ):
            register_terminal_tools(
                registry,
                session_manager,
                MagicMock(fetchone=MagicMock(return_value=None)),
                terminal_manager=terminal_manager,
                terminal_runtime_registry=runtime_registry,
            )

        capture_output = registry.get_tool("capture_output")
        assert capture_output is not None
        result = asyncio.run(capture_output(session_id="session-1", lines=20))

        assert result["success"] is True
        assert result["via"] == "native"
        assert result["output"] == "live pane"


def test_send_keys_rejects_raw_tmux_context_after_native_lookup() -> None:
    TestRegisterTerminalTools().assert_send_keys_rejects_raw_tmux_context_after_native_lookup()


@pytest.mark.parametrize("stage", ["none", "partial"])
@pytest.mark.parametrize("keys,literal", [("/compact\n", True), ("enter", False)])
def test_send_keys_reports_terminal_write_refusal(stage: str, keys: str, literal: bool) -> None:
    send_keys, _, coordinator = TestRegisterTerminalTools._authorized_send_keys()
    error = TerminalWriteError(stage="partial" if stage == "partial" else "none")
    coordinator.write.side_effect = error
    with patch("gobby.utils.session_context.get_current_session_id", return_value="caller-session"):
        result = asyncio.run(
            send_keys(
                session_id="target-session", keys=keys, literal=literal, idempotency_key="compact-1"
            )
        )
    assert result["success"] is False
    assert result["error_code"] == "terminal_write_failed"
    assert result["stage"] == stage
    assert result["indeterminate"] is (stage == "partial")
    assert result["idempotency_key"] == "compact-1"
    request = coordinator.write.await_args.args[0]
    assert request.idempotency_key == "compact-1"
    assert request.payload == ("/compact" if literal else "enter")
    assert request.submit is literal
    coordinator.write.assert_awaited_once()


def test_send_keys_named_key_never_pastes_literally() -> None:
    registry = _TestRegistry(name="test", description="test")
    session = MagicMock(
        id="session-1",
        project_id="project-1",
        agent_run_id=None,
        terminal_context=None,
    )
    session_manager = MagicMock()
    session_manager.get.return_value = session
    session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
    terminal_manager = MagicMock()
    terminal_manager.resolve_live_for_session.return_value = SimpleNamespace(
        id="terminal-1", backend="native"
    )
    write_coordinator = MagicMock()
    write_coordinator.write = AsyncMock(return_value=Delivered())

    register_terminal_tools(
        registry,
        session_manager,
        MagicMock(fetchone=MagicMock(return_value=None)),
        terminal_manager=terminal_manager,
        write_coordinator=write_coordinator,
    )

    send_keys = registry.get_tool("send_keys")
    assert send_keys is not None
    with patch("gobby.utils.session_context.get_current_session_id", return_value="session-1"):
        result = asyncio.run(send_keys(session_id="session-1", keys="C-u", literal=False))

    assert result["success"] is True
    request = write_coordinator.write.await_args.args[0]
    assert request.kind == "key"
    assert request.payload == "ctrl_u"

    with patch("gobby.utils.session_context.get_current_session_id", return_value="session-1"):
        unsupported = asyncio.run(
            send_keys(session_id="session-1", keys="not-a-native-key", literal=False)
        )

    assert unsupported["success"] is False
    assert unsupported["error_code"] == "send_keys_named_key_unsupported"
    assert write_coordinator.write.await_count == 1


class TestSetHandoffFeedback:
    def test_clear_waits_for_persisted_task_closure(
        self, temp_db: HubDatabase, tmp_path: Path
    ) -> None:
        manager, session_id = _persistent_session(temp_db, tmp_path)
        session = manager.get(session_id)
        assert session is not None
        tasks = LocalTaskManager(temp_db)
        task = tasks.create_task(
            project_id=session.project_id,
            title="Handoff boundary task",
            validation_criteria="Clearing is blocked until this task closes.",
            claimed_by_session_id=session_id,
        )
        registry = _feedback_registry(temp_db, manager)
        set_handoff = registry.get_tool("set_handoff")
        feedback = registry.get_tool("feedback")
        assert set_handoff is not None and feedback is not None
        with (
            session_context_for_test(session_id),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal_clear.prepare_clear_session",
                new_callable=AsyncMock,
                return_value={"handoff_staged": True},
            ) as stage_clear,
        ):
            assert feedback(observations=[])["success"] is True
            blocked = asyncio.run(
                set_handoff(current_state="Ready", next_steps=["Continue"], clear_session=True)
            )
            assert blocked["error_code"] == "active_task_requires_compact"
            assert "clear_session=false" in blocked["error"]
            assert _handoff_row_count(temp_db, session_id) == 0
            stage_clear.assert_not_awaited()
            tasks.close_task(task.id)
            allowed = asyncio.run(
                set_handoff(current_state="Closed", next_steps=["Next task"], clear_session=True)
            )
            assert allowed["handoff_staged"] is True
            stage_clear.assert_awaited_once()

    """Separate feedback submission is a prerequisite, never human review."""

    def test_authoring_guidance_comes_from_installed_prompt(
        self, temp_db: HubDatabase, tmp_path: Path
    ) -> None:
        from gobby.prompts.loader import PromptLoader
        from gobby.prompts.sync import sync_bundled_prompts

        sync_bundled_prompts(temp_db)
        manager, _session_id = _persistent_session(temp_db, tmp_path)
        registry = _feedback_registry(temp_db, manager)
        metadata = registry.get_tool_metadata("set_handoff")
        assert metadata is not None
        prompt = PromptLoader(db=temp_db).load("handoff/authoring").content
        assert metadata.description == prompt
        assert "current context epoch only" in prompt
        assert "Earlier history is stored in the database" in prompt
        assert "Do not overuse" in prompt
        assert "including resolved friction" in prompt
        assert "Never copy earlier reflections" in prompt
        assert "project-relative path to references" in prompt
        assert "5,000 encoded characters" in prompt
        assert "call set_handoff last" in prompt

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
        registry = _feedback_registry(temp_db, manager)
        set_handoff = registry.get_tool("set_handoff")
        feedback = registry.get_tool("feedback")
        assert set_handoff is not None and feedback is not None
        with session_context_for_test(session_id):
            blocked = asyncio.run(
                set_handoff(
                    current_state="x" * 10_001, next_steps=["Continue"], clear_session=clear_session
                )
            )
            assert blocked["error_code"] == "feedback_required"
            assert _handoff_row_count(temp_db, session_id) == 0
            assert feedback(observations=[])["success"] is True
        before = SessionVariableManager(temp_db).get_variables(session_id)
        with session_context_for_test(session_id):
            result = asyncio.run(
                set_handoff(
                    current_state="x" * 10_001, next_steps=["Continue"], clear_session=clear_session
                )
            )
        assert result["error_code"] == "invalid_handoff"
        assert "Shorten the inline handoff and retry" in result["error"]
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


def _set_handoff_completion(session_id: str, result: dict[str, Any]) -> HookEvent:
    """Build the AFTER_TOOL event the proxy hands the CLI for a set_handoff result."""
    data: dict[str, Any] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {
            "server_name": "gobby-sessions",
            "tool_name": "set_handoff",
            "arguments": {"clear_session": False},
        },
        "tool_output": {
            "success": True,
            "result": {key: value for key, value in result.items() if key != "success"},
        },
    }
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="provider-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": session_id},
    )


def test_set_handoff_retry_reuses_the_in_flight_compact_attempt(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    """A retry while /compact is in flight never stages or dispatches a sibling (#23495).

    Both incidents staged a second attempt over a dispatched one. Its delivery hit
    "compact boundary wait already active", raised handoff_delivery_failed, and left
    the retry gate demanding set_handoff after the first attempt compacted the session,
    while the continuation prompt said not to call set_handoff again.
    """
    manager, session_id = _persistent_session(temp_db, tmp_path)
    set_handoff = _feedback_registry(temp_db, manager, survey="off").get_tool("set_handoff")
    assert set_handoff is not None
    pane = MagicMock(backend="tmux", target="%12")
    pane.snapshot = AsyncMock(return_value="ready")
    variables = SessionVariableManager(temp_db)
    event_loop = MagicMock()
    event_loop.is_closed.return_value = False

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
        first = asyncio.run(set_handoff(current_state="First", next_steps=["Continue"]))
        # The first attempt's delivery is in flight: claimed and its /compact typed.
        claimed = claim_staged_handoff_delivery(
            temp_db, session_id, first["attempt_id"], recover_unarmed_gate=True
        )
        assert claimed is not None
        retry = asyncio.run(set_handoff(current_state="Retry", next_steps=["Continue"]))

    assert (retry["handoff_staged"], retry["delivery_pending"]) == (True, True)
    assert retry["reused_attempt"] is True
    assert retry["attempt_id"] == first["attempt_id"]
    assert _handoff_row_count(temp_db, session_id) == 1
    marker = variables.get_variables(session_id)[PENDING_HANDOFF_VARIABLE]
    assert (marker["attempt_id"], marker["handoff_record_id"]) == (
        claimed.attempt_id,
        claimed.handoff_record_id,
    )

    # The context observer re-arms the gate from the retry result; AFTER_TOOL then
    # schedules it, and the in-flight claim leaves nothing to dispatch.
    pending_gate = {
        "handoff_staged": True,
        "delivery_pending": True,
        "attempt_id": retry["attempt_id"],
        "clear_session": False,
    }
    variables.merge_variables(session_id, {HANDOFF_DISPATCH_GATE_VARIABLE: pending_gate})
    with patch("gobby.hooks.terminal_handoff_delivery.asyncio.run_coroutine_threadsafe") as submit:
        scheduled = schedule_terminal_handoff_delivery(
            _set_handoff_completion(session_id, retry),
            session_manager=manager,
            agent_run_manager=MagicMock(),
            event_loop=event_loop,
        )
    assert scheduled is False
    submit.assert_not_called()

    # The first attempt compacts. Its gate is the pending one the compact boundary
    # releases, never a delivery_failed gate demanding another set_handoff.
    assert record_handoff_delivery(
        temp_db,
        handoff_id=claimed.handoff_record_id,
        attempt_id=claimed.attempt_id,
        boundary_kind="compact",
        continuation_session_id=session_id,
    )
    settled = variables.get_variables(session_id)
    assert settled[HANDOFF_DISPATCH_GATE_VARIABLE] == pending_gate
    assert FAILED_HANDOFF_VARIABLE not in settled
    assert HANDOFF_DELIVERY_FAILURES_VARIABLE not in settled
    consumed = consume_pending_handoff(temp_db, session_id)
    assert consumed is not None
    assert "First" in consumed.markdown
