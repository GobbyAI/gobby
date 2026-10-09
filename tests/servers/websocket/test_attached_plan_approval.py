"""Tests for attached native CLI plan approval through the managed terminal."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.adapters.plan_keystrokes import (
    REQUEST_CHANGES_OPTION_ID,
    PlanKeystroke,
    PlanKeystrokeRegistry,
    PlanKeystrokeSequence,
    build_default_plan_keystroke_registry,
)
from gobby.servers.websocket.handlers.plan_approval import (
    handle_attached_plan_approval,
    handle_plan_approval_response,
)
from gobby.servers.websocket.session_control import SessionControlMixin
from gobby.terminals.runtime import Delivered, IndeterminateWrite, Suppressed
from gobby.terminals.write_coordinator import SequenceDelay


@contextmanager
def _wire_native(server: ConcreteSessionControl, probe: MagicMock) -> Any:
    terminal = SimpleNamespace(id="native-1", backend="native")
    server.terminal_manager = MagicMock()
    server.terminal_manager.resolve_live_for_session.return_value = terminal

    async def snapshot(_terminal: Any, *, lines: int) -> Any:
        text = await probe.capture_pane("native-1", lines=lines)
        return SimpleNamespace(text=text)

    runtime = MagicMock()
    runtime.snapshot = AsyncMock(side_effect=snapshot)
    server.terminal_runtime_registry = MagicMock()
    server.terminal_runtime_registry.resolve.return_value = runtime

    async def run_sequence(_terminal_id: str, *, steps: Any, **_kwargs: Any) -> Any:
        for step in steps:
            if isinstance(step, SequenceDelay):
                continue
            if step.kind == "key":
                key = {"enter": "Enter", "escape": "Escape"}.get(step.payload, step.payload)
                literal = False
            elif step.payload == "\x12":
                key, literal = "C-r", False
            else:
                key, literal = step.payload, True
            if not await probe.send_keys("native-1", key, literal=literal):
                return Suppressed(action_key="plan-approval")
        return Delivered()

    server.write_coordinator = MagicMock()
    server.write_coordinator.run_sequence = AsyncMock(side_effect=run_sequence)
    yield server.terminal_manager.resolve_live_for_session


# Trimmed verbatim Claude Code v2.1.169 captures (full plan menu vs. bare confirm).
_CLAUDE_FULL_MENU_PANE = (
    "Claude has written up a plan and is ready to execute. Would you like to proceed?\n"
    " 1. Yes, and use auto mode\n"
    " 2. Yes, manually approve edits\n"
    " 3. No, refine with Ultraplan on Claude Code on the web\n"
    " 4. Tell Claude what to change\n"
)
_CLAUDE_CONFIRM_MENU_PANE = "Exit plan mode?\n Claude wants to exit plan mode\n 1. Yes\n 2. No\n"
_CODEX_PLAN_MENU_PANE = (
    "Implement this plan?\n1. Yes, implement this plan\n3. No, stay in Plan mode\n"
)
_DROID_PLAN_MENU_PANE = (
    "1. Proceed with the proposal\n4. No and explain why\nup/down navigate   1-4 select\n"
)
_GROK_PLAN_MENU_PANE = "1 [*] Yes, and don't ask again\n4 [ ] No, reject (type to add feedback)\n"
_AGY_PLAN_MENU_PANE = "Action required\n1. Approve\n2. Reject\n"


class ConcreteSessionControl(SessionControlMixin):
    """Minimal concrete SessionControlMixin for handler tests."""

    def __init__(self) -> None:
        self.clients: dict[Any, dict[str, Any]] = {}
        self._chat_sessions: dict[str, Any] = {}
        self._active_chat_tasks: dict[str, Any] = {}
        self._pending_modes: dict[str, str] = {}
        self._pending_worktree_paths: dict[str, str] = {}
        self._pending_agents: dict[str, str] = {}
        self._pending_projects: dict[str, str] = {}
        self._pending_providers: dict[str, str] = {}
        self._send_error = AsyncMock()
        self.session_manager = MagicMock()


def _make_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send = AsyncMock()
    return ws


def _make_terminal_session(
    *,
    source: str = "example",
    tmux_pane: str | None = "%11",
    session_type: str = "terminal",
) -> MagicMock:
    session = MagicMock()
    session.session_type = session_type
    session.source = source
    session.terminal_context = {"tmux_pane": tmux_pane} if tmux_pane else {}
    session.metadata = None
    return session


def _registry_with(
    source: str, option_id: str, seq: PlanKeystrokeSequence
) -> PlanKeystrokeRegistry:
    registry = PlanKeystrokeRegistry()
    registry.register(source, option_id, seq)
    return registry


class TestAttachedPlanApprovalDispatch:
    @pytest.mark.asyncio
    async def test_approve_dispatches_registered_keystrokes(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            tmux_pane=None
        )

        registry = _registry_with(
            "example",
            "approve_yolo",
            PlanKeystrokeSequence(
                strokes=(PlanKeystroke("1", literal=True), PlanKeystroke("Enter")),
                settle_seconds=0.0,
            ),
        )
        tmux_manager = MagicMock()
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager) as resolve_terminal:
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_yolo"},
                registry=registry,
            )

        resolve_terminal.assert_called_once_with(
            cast(MagicMock, server.session_manager).get.return_value
        )
        server.write_coordinator.run_sequence.assert_awaited_once()
        assert tmux_manager.send_keys.await_args_list[0].args == ("native-1", "1")
        assert tmux_manager.send_keys.await_args_list[0].kwargs == {"literal": True}
        assert tmux_manager.send_keys.await_args_list[1].args == ("native-1", "Enter")
        assert tmux_manager.send_keys.await_args_list[1].kwargs == {"literal": False}
        msg = json.loads(ws.send.await_args.args[0])
        assert msg == {
            "type": "plan_approval_dispatched",
            "target_session_id": "term-1",
            "decision": "approve",
            "option_id": "approve_yolo",
            "ok": True,
        }
        cast(AsyncMock, server._send_error).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_request_changes_dispatches_keep_planning(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session()

        registry = _registry_with(
            "example",
            REQUEST_CHANGES_OPTION_ID,
            PlanKeystrokeSequence(strokes=(PlanKeystroke("3", literal=True),), settle_seconds=0.0),
        )
        tmux_manager = MagicMock()
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "request_changes", "feedback": "tighten it"},
                registry=registry,
            )

        tmux_manager.send_keys.assert_awaited_once_with("native-1", "3", literal=True)
        msg = json.loads(ws.send.await_args.args[0])
        assert msg["decision"] == "request_changes"
        assert msg["option_id"] == REQUEST_CHANGES_OPTION_ID
        cast(AsyncMock, server._send_error).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unmapped_source_errors(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="claude"
        )

        with _wire_native(server, MagicMock()):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_yolo"},
                registry=PlanKeystrokeRegistry(),
            )

        cast(AsyncMock, server._send_error).assert_awaited_once()
        assert (
            cast(AsyncMock, server._send_error).await_args.kwargs.get("code")
            == "PLAN_KEYSTROKES_UNMAPPED"
        )
        ws.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_terminal_session_errors(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            session_type="web_chat"
        )

        await handle_attached_plan_approval(
            server,
            ws,
            "term-1",
            {"decision": "approve", "option_id": "approve_yolo"},
            registry=PlanKeystrokeRegistry(),
        )

        cast(AsyncMock, server._send_error).assert_awaited_once()
        assert (
            cast(AsyncMock, server._send_error).await_args.kwargs.get("code")
            == "UNSUPPORTED_SESSION_TYPE"
        )

    @pytest.mark.asyncio
    async def test_missing_managed_terminal_errors(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            tmux_pane=None
        )

        await handle_attached_plan_approval(
            server,
            ws,
            "term-1",
            {"decision": "approve", "option_id": "approve_yolo"},
            registry=PlanKeystrokeRegistry(),
        )

        cast(AsyncMock, server._send_error).assert_awaited_once()
        assert (
            cast(AsyncMock, server._send_error).await_args.kwargs.get("code")
            == "NO_TERMINAL_TARGET"
        )

    @pytest.mark.asyncio
    async def test_live_legacy_terminal_is_fenced(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session()
        probe = MagicMock()

        with _wire_native(server, probe):
            server.terminal_manager.resolve_live_for_session.return_value.backend = "tmux"
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_yolo"},
                registry=_registry_with(
                    "example",
                    "approve_yolo",
                    PlanKeystrokeSequence(strokes=(PlanKeystroke("1", literal=True),)),
                ),
            )

        assert (
            cast(AsyncMock, server._send_error).await_args.kwargs["code"]
            == "UNSUPPORTED_TERMINAL_BACKEND"
        )
        server.write_coordinator.run_sequence.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_indeterminate_native_write_is_not_reported_dispatched(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            tmux_pane=None
        )

        with _wire_native(server, MagicMock()):
            server.write_coordinator.run_sequence = AsyncMock(
                return_value=IndeterminateWrite("lost response")
            )
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_yolo"},
                registry=_registry_with(
                    "example",
                    "approve_yolo",
                    PlanKeystrokeSequence(strokes=(PlanKeystroke("1", literal=True),)),
                ),
            )

        assert (
            cast(AsyncMock, server._send_error).await_args.kwargs["code"]
            == "PLAN_DISPATCH_UNCONFIRMED"
        )
        ws.send.assert_not_awaited()

    async def test_in_flight_dispatch_timeout_is_unconfirmed(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            tmux_pane=None
        )
        started = asyncio.Event()

        async def dispatch_in_flight(*_args: Any, **_kwargs: Any) -> Delivered:
            started.set()
            await asyncio.Event().wait()
            return Delivered()

        with (
            _wire_native(server, MagicMock()),
            patch(
                "gobby.servers.websocket.handlers.plan_approval."
                "_PLAN_TERMINAL_OPERATION_TIMEOUT_SECONDS",
                0.01,
            ),
        ):
            server.write_coordinator.run_sequence = AsyncMock(side_effect=dispatch_in_flight)
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_yolo"},
                registry=_registry_with(
                    "example",
                    "approve_yolo",
                    PlanKeystrokeSequence(strokes=(PlanKeystroke("1", literal=True),)),
                ),
            )

        assert started.is_set()
        assert (
            cast(AsyncMock, server._send_error).await_args.kwargs["code"]
            == "PLAN_DISPATCH_UNCONFIRMED"
        )
        ws.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_decision_errors(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session()

        # approve without an option_id cannot resolve which menu item to select.
        await handle_attached_plan_approval(
            server,
            ws,
            "term-1",
            {"decision": "approve"},
            registry=PlanKeystrokeRegistry(),
        )

        cast(AsyncMock, server._send_error).assert_awaited_once()
        assert (
            cast(AsyncMock, server._send_error).await_args.kwargs.get("code")
            == "INVALID_PLAN_DECISION"
        )

    @pytest.mark.asyncio
    async def test_session_not_found_errors(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = None

        await handle_attached_plan_approval(
            server,
            ws,
            "term-1",
            {"decision": "approve", "option_id": "approve_yolo"},
            registry=PlanKeystrokeRegistry(),
        )

        cast(AsyncMock, server._send_error).assert_awaited_once()
        assert cast(AsyncMock, server._send_error).await_args.kwargs.get("code") == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_failed_send_reports_error(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session()

        registry = _registry_with(
            "example",
            "approve_act",
            PlanKeystrokeSequence(strokes=(PlanKeystroke("2", literal=True),), settle_seconds=0.0),
        )
        tmux_manager = MagicMock()
        tmux_manager.send_keys = AsyncMock(return_value=False)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_act"},
                registry=registry,
            )

        cast(AsyncMock, server._send_error).assert_awaited_once()
        # The surfaced error names the keystroke-send failure, and no
        # confirmation frame is emitted when the dispatch did not complete.
        assert "keystrokes" in cast(AsyncMock, server._send_error).await_args.args[1]
        ws.send.assert_not_awaited()


class TestAttachedPlanApprovalClaude:
    """Claude's pane-aware path: capture the live menu, then pick keys per shape."""

    @pytest.mark.asyncio
    async def test_full_menu_approve_yolo_captures_and_dispatches(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="claude"
        )

        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value=_CLAUDE_FULL_MENU_PANE)
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_yolo"},
                registry=build_default_plan_keystroke_registry(),
            )

        # Live pane was captured to disambiguate the menu shape.
        tmux_manager.capture_pane.assert_awaited_once()
        assert tmux_manager.capture_pane.await_args.args[0] == "native-1"
        # Full menu: digit '1' then Enter to activate.
        assert tmux_manager.send_keys.await_args_list[0].args == ("native-1", "1")
        assert tmux_manager.send_keys.await_args_list[0].kwargs == {"literal": True}
        assert tmux_manager.send_keys.await_args_list[1].args == ("native-1", "Enter")
        assert tmux_manager.send_keys.await_args_list[1].kwargs == {"literal": False}
        msg = json.loads(ws.send.await_args.args[0])
        assert msg["option_id"] == "approve_yolo"
        assert msg["ok"] is True
        cast(AsyncMock, server._send_error).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_confirm_menu_request_changes_sends_digit_only(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="claude"
        )

        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value=_CLAUDE_CONFIRM_MENU_PANE)
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "request_changes"},
                registry=build_default_plan_keystroke_registry(),
            )

        # The live pane was read to pick the confirm-menu mapping.
        tmux_manager.capture_pane.assert_awaited_once()
        # Bare confirm menu activates on the digit alone -- no trailing Enter.
        tmux_manager.send_keys.assert_awaited_once_with("native-1", "2", literal=True)
        assert ws.send.await_count == 1
        msg = json.loads(ws.send.await_args.args[0])
        assert msg == {
            "type": "plan_approval_dispatched",
            "target_session_id": "term-1",
            "decision": "request_changes",
            "option_id": REQUEST_CHANGES_OPTION_ID,
            "ok": True,
        }
        cast(AsyncMock, server._send_error).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_menu_on_pane_reports_unmapped(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="claude"
        )

        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value="just a shell prompt, no menu\n")
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_yolo"},
                registry=build_default_plan_keystroke_registry(),
            )

        # The pane was captured, found no menu, and no keystrokes were guessed.
        tmux_manager.capture_pane.assert_awaited_once()
        cast(AsyncMock, server._send_error).assert_awaited_once()
        assert (
            cast(AsyncMock, server._send_error).await_args.kwargs.get("code")
            == "PLAN_KEYSTROKES_UNMAPPED"
        )
        # The surfaced error names the source and the unmapped option.
        error_message = cast(AsyncMock, server._send_error).await_args.args[1]
        assert "claude" in error_message
        assert "approve_yolo" in error_message
        tmux_manager.send_keys.assert_not_awaited()
        ws.send.assert_not_awaited()


class TestAttachedPlanApprovalCodex:
    """Codex's static single-shape path: menu-guarded digit-only dispatch."""

    @pytest.mark.asyncio
    async def test_approve_dispatches_digit_only_with_menu_guard(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="codex"
        )

        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value=_CODEX_PLAN_MENU_PANE)
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_act"},
                registry=build_default_plan_keystroke_registry(),
            )

        tmux_manager.capture_pane.assert_awaited_once()
        # Codex plan menu activates on the digit alone; approve maps to "1".
        tmux_manager.send_keys.assert_awaited_once_with("native-1", "1", literal=True)
        msg = json.loads(ws.send.await_args.args[0])
        assert msg["option_id"] == "approve_act"
        assert msg["ok"] is True
        cast(AsyncMock, server._send_error).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_request_changes_dispatches_stay_in_plan_mode(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="codex"
        )

        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value=_CODEX_PLAN_MENU_PANE)
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "request_changes"},
                registry=build_default_plan_keystroke_registry(),
            )

        tmux_manager.capture_pane.assert_awaited_once()
        # request-changes maps to "3" (No, stay in Plan mode), digit only.
        tmux_manager.send_keys.assert_awaited_once_with("native-1", "3", literal=True)
        assert ws.send.await_count == 1
        msg = json.loads(ws.send.await_args.args[0])
        assert msg == {
            "type": "plan_approval_dispatched",
            "target_session_id": "term-1",
            "decision": "request_changes",
            "option_id": REQUEST_CHANGES_OPTION_ID,
            "ok": True,
        }

    @pytest.mark.asyncio
    async def test_stale_approval_click_without_menu_sends_no_keystrokes(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="codex"
        )

        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value="agent is still generating\n")
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_act"},
                registry=build_default_plan_keystroke_registry(),
            )

        tmux_manager.capture_pane.assert_awaited_once()
        tmux_manager.send_keys.assert_not_awaited()
        cast(AsyncMock, server._send_error).assert_awaited_once()
        assert (
            cast(AsyncMock, server._send_error).await_args.kwargs.get("code")
            == "PLAN_KEYSTROKES_UNMAPPED"
        )
        error_message = cast(AsyncMock, server._send_error).await_args.args[1]
        assert "codex" in error_message
        assert "approve_act" in error_message
        ws.send.assert_not_awaited()


class TestAttachedPlanApprovalDroid:
    """Droid's static single-shape spec-menu path: menu-guarded digit only."""

    @pytest.mark.asyncio
    async def test_approve_dispatches_digit_only_with_menu_guard(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="droid"
        )

        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value=_DROID_PLAN_MENU_PANE)
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_act"},
                registry=build_default_plan_keystroke_registry(),
            )

        tmux_manager.capture_pane.assert_awaited_once()
        # Droid spec menu activates on the digit alone; approve maps to "1".
        tmux_manager.send_keys.assert_awaited_once_with("native-1", "1", literal=True)
        msg = json.loads(ws.send.await_args.args[0])
        assert msg["option_id"] == "approve_act"
        assert msg["ok"] is True
        cast(AsyncMock, server._send_error).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_request_changes_dispatches_no_and_explain(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="droid"
        )

        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value=_DROID_PLAN_MENU_PANE)
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "request_changes"},
                registry=build_default_plan_keystroke_registry(),
            )

        tmux_manager.capture_pane.assert_awaited_once()
        # request-changes maps to "4" (No and explain why), digit only.
        tmux_manager.send_keys.assert_awaited_once_with("native-1", "4", literal=True)
        assert ws.send.await_count == 1
        msg = json.loads(ws.send.await_args.args[0])
        assert msg == {
            "type": "plan_approval_dispatched",
            "target_session_id": "term-1",
            "decision": "request_changes",
            "option_id": REQUEST_CHANGES_OPTION_ID,
            "ok": True,
        }
        cast(AsyncMock, server._send_error).assert_not_awaited()


class TestAttachedPlanApprovalUnsupported:
    """Unsupported sources have no plan-approval keystrokes."""

    @pytest.mark.asyncio
    async def test_unsupported_source_is_unmapped(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="unsupported"
        )

        with _wire_native(server, MagicMock()):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_yolo"},
                registry=build_default_plan_keystroke_registry(),
            )

        cast(AsyncMock, server._send_error).assert_awaited_once()
        assert (
            cast(AsyncMock, server._send_error).await_args.kwargs.get("code")
            == "PLAN_KEYSTROKES_UNMAPPED"
        )
        ws.send.assert_not_awaited()


class TestAttachedPlanApprovalGrok:
    """Grok's ("Grok Build" TUI) guarded static approval-menu path:
    positionally-stable digits (1 yolo / 3 approve / 4 reject), every action a
    single immediate literal digit (reject is digit 4, not Esc)."""

    @pytest.mark.asyncio
    async def test_approve_act_dispatches_digit_three_with_menu_guard(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="grok"
        )

        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value=_GROK_PLAN_MENU_PANE)
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_act"},
                registry=build_default_plan_keystroke_registry(),
            )

        tmux_manager.capture_pane.assert_awaited_once()
        # approve_act ("Yes, proceed"/"Yes", single approval) maps to "3".
        tmux_manager.send_keys.assert_awaited_once_with("native-1", "3", literal=True)
        msg = json.loads(ws.send.await_args.args[0])
        assert msg["option_id"] == "approve_act"
        assert msg["ok"] is True
        cast(AsyncMock, server._send_error).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approve_yolo_dispatches_digit_one_with_menu_guard(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="grok"
        )

        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value=_GROK_PLAN_MENU_PANE)
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_yolo"},
                registry=build_default_plan_keystroke_registry(),
            )

        tmux_manager.capture_pane.assert_awaited_once()
        # approve_yolo ("always-approve mode", bypass) maps to "1".
        tmux_manager.send_keys.assert_awaited_once_with("native-1", "1", literal=True)
        msg = json.loads(ws.send.await_args.args[0])
        assert msg["option_id"] == "approve_yolo"
        assert msg["ok"] is True
        cast(AsyncMock, server._send_error).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_request_changes_dispatches_reject_digit_four(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="grok"
        )

        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value=_GROK_PLAN_MENU_PANE)
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "request_changes"},
                registry=build_default_plan_keystroke_registry(),
            )

        tmux_manager.capture_pane.assert_awaited_once()
        # request-changes is the stable reject digit "4" (literal) -- grok's "No,
        # reject" item is identical across menu shapes; Esc only unselects.
        tmux_manager.send_keys.assert_awaited_once_with("native-1", "4", literal=True)
        assert ws.send.await_count == 1
        msg = json.loads(ws.send.await_args.args[0])
        assert msg == {
            "type": "plan_approval_dispatched",
            "target_session_id": "term-1",
            "decision": "request_changes",
            "option_id": REQUEST_CHANGES_OPTION_ID,
            "ok": True,
        }
        cast(AsyncMock, server._send_error).assert_not_awaited()


class TestPlanApprovalRouting:
    @pytest.mark.asyncio
    async def test_target_session_id_routes_to_attached_handler(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        with patch(
            "gobby.servers.websocket.handlers.plan_approval.handle_attached_plan_approval",
            new=AsyncMock(),
        ) as attached:
            await handle_plan_approval_response(
                server,
                ws,
                {
                    "target_session_id": "term-1",
                    "decision": "approve",
                    "option_id": "approve_yolo",
                },
            )

        attached.assert_awaited_once()
        assert attached.await_args is not None
        assert attached.await_args.args[2] == "term-1"

    @pytest.mark.asyncio
    async def test_no_target_session_id_uses_conversation_path(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        with patch(
            "gobby.servers.websocket.handlers.plan_approval.handle_attached_plan_approval",
            new=AsyncMock(),
        ) as attached:
            # Unknown conversation falls through to recovery; the attached path
            # must not be invoked when target_session_id is absent.
            with patch(
                "gobby.servers.websocket.handlers.plan_approval.handle_recovered_plan_approval",
                new=AsyncMock(),
            ) as recovered:
                await handle_plan_approval_response(
                    server,
                    ws,
                    {"conversation_id": "conv-1", "decision": "approve"},
                )

        # Routing went to the conversation/recovery branch, not the attached one.
        recovered.assert_awaited_once()
        assert recovered.await_args is not None
        assert recovered.await_args.args[2] == "conv-1"
        assert ws.send.await_count == 0
        attached.assert_not_awaited()


class TestAttachedPlanApprovalAgy:
    """AGY artifact review: Action required header, C-r then y/n."""

    @pytest.mark.asyncio
    async def test_approve_dispatches_ctrl_r_then_y(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="agy"
        )

        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value=_AGY_PLAN_MENU_PANE)
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_act"},
                registry=build_default_plan_keystroke_registry(),
            )

        assert [call.args for call in tmux_manager.send_keys.await_args_list] == [
            ("native-1", "C-r"),
            ("native-1", "y"),
        ]
        assert [call.kwargs["literal"] for call in tmux_manager.send_keys.await_args_list] == [
            False,
            True,
        ]
        send = cast(AsyncMock, ws.send)
        assert send.await_args is not None
        msg = json.loads(send.await_args.args[0])
        assert msg["option_id"] == "approve_act"
        assert msg["ok"] is True
        cast(AsyncMock, server._send_error).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_request_changes_dispatches_ctrl_r_then_n(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        cast(MagicMock, server.session_manager).get.return_value = _make_terminal_session(
            source="agy"
        )

        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value=_AGY_PLAN_MENU_PANE)
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with _wire_native(server, tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "request_changes"},
                registry=build_default_plan_keystroke_registry(),
            )

        assert [call.args for call in tmux_manager.send_keys.await_args_list] == [
            ("native-1", "C-r"),
            ("native-1", "n"),
        ]
        assert [call.kwargs["literal"] for call in tmux_manager.send_keys.await_args_list] == [
            False,
            True,
        ]
        cast(AsyncMock, server._send_error).assert_not_awaited()
