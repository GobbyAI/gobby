"""Provider submit records and dialog waits reach the composer ledger."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.sessions.turn_lifecycle import TurnDisposition, WaitResolution
from gobby.terminals.composer_ledger import ComposerLedger, LedgerRead

pytestmark = pytest.mark.unit

_SESSION = "0f8c1a52-7d3e-4c1b-9a6e-2b5d8f4e1c30"
_TERMINAL = "terminal-1"
_EMPTY = LedgerRead("empty")
_DRAFT = LedgerRead("draft")


@pytest.fixture
def ledger() -> Iterator[ComposerLedger]:
    ledger = ComposerLedger()
    ledger.release(_TERMINAL)
    app = SimpleNamespace(write_coordinator=SimpleNamespace(composer_ledger=ledger))
    with patch("gobby.hooks.event_handlers._base.get_app_context", return_value=app):
        yield ledger


@pytest.fixture
def terminals() -> MagicMock:
    terminals = MagicMock(name="terminal_manager")
    terminals.get_live_for_session.side_effect = lambda session_id: (
        SimpleNamespace(id=_TERMINAL, backend="native") if session_id == _SESSION else None
    )
    return terminals


@pytest.fixture
def handlers(mock_dependencies: dict[str, Any], terminals: MagicMock) -> EventHandlers:
    return EventHandlers(terminal_manager=terminals, **mock_dependencies)


def _event(
    event_type: HookEventType,
    data: dict[str, Any] | None = None,
    *,
    metadata: dict[str, Any] | None = None,
    wait_resolution: WaitResolution | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="ext-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(),
        data=data or {},
        metadata={"_platform_session_id": _SESSION, **(metadata or {})},
        wait_token="wait-1",
        wait_resolution=wait_resolution,
    )


def test_prompt_submit_consumes_the_typed_prompt(
    handlers: EventHandlers, ledger: ComposerLedger
) -> None:
    ledger.observe_write(_TERMINAL, origin="operator", kind="text", payload="fix it", submit=True)
    assert ledger.read(_TERMINAL) == _DRAFT

    handlers.handle_before_agent(_event(HookEventType.BEFORE_AGENT, {"prompt": "fix it"}))

    assert ledger.read(_TERMINAL) == _EMPTY


@pytest.mark.parametrize(
    ("backend", "after"),
    [("native", _EMPTY), ("tmux", LedgerRead("blocked", "untracked"))],
    ids=["native-adopted", "tmux-unobserved"],
)
def test_prompt_submit_adopts_an_untracked_seat_only_when_its_input_is_observed(
    handlers: EventHandlers,
    ledger: ComposerLedger,
    terminals: MagicMock,
    backend: str,
    after: LedgerRead,
) -> None:
    terminals.get_live_for_session.side_effect = None
    terminals.get_live_for_session.return_value = SimpleNamespace(
        id="pre-ledger-seat", backend=backend
    )

    handlers.handle_before_agent(_event(HookEventType.BEFORE_AGENT, {"prompt": "continue"}))

    assert ledger.read("pre-ledger-seat") == after


def test_native_child_prompt_leaves_the_parent_composer_alone(
    handlers: EventHandlers, ledger: ComposerLedger
) -> None:
    ledger.observe_write(_TERMINAL, origin="operator", kind="text", payload="fix it", submit=True)

    handlers.handle_before_agent(
        _event(
            HookEventType.BEFORE_AGENT,
            {"prompt": "child task"},
            metadata={"_native_subagent_binding": True},
        )
    )

    assert ledger.read(_TERMINAL) == _DRAFT


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [("manual", _EMPTY), ("auto", LedgerRead("held", pending="/compact\r"))],
)
def test_manual_compaction_consumes_the_daemon_typed_compact(
    handlers: EventHandlers, ledger: ComposerLedger, trigger: str, expected: LedgerRead
) -> None:
    ledger.observe_write(_TERMINAL, origin="daemon", kind="text", payload="/compact\r", submit=True)

    handlers.handle_pre_compact(_event(HookEventType.PRE_COMPACT, {"trigger": trigger}))

    assert ledger.read(_TERMINAL) == expected


@pytest.mark.parametrize(("resolution", "expected"), [("resumed", _EMPTY), ("abandoned", _DRAFT)])
def test_dialog_input_follows_how_the_wait_resolved(
    handlers: EventHandlers,
    ledger: ComposerLedger,
    resolution: WaitResolution,
    expected: LedgerRead,
) -> None:
    handlers.handle_elicitation(_event(HookEventType.ELICITATION))
    ledger.observe_write(_TERMINAL, origin="operator", kind="text", payload="y", submit=True)

    handlers.handle_elicitation_result(
        _event(HookEventType.ELICITATION_RESULT, wait_resolution=resolution)
    )

    assert ledger.read(_TERMINAL) == expected


def test_tool_work_after_an_approval_closes_the_dialog(
    handlers: EventHandlers, ledger: ComposerLedger
) -> None:
    handlers.handle_permission_request(_event(HookEventType.PERMISSION_REQUEST))
    ledger.observe_write(_TERMINAL, origin="operator", kind="key", payload="enter")

    handlers.handle_before_tool(_event(HookEventType.BEFORE_TOOL, {"tool_name": "Bash"}))

    assert ledger.read(_TERMINAL) == _EMPTY
    ledger.observe_write(_TERMINAL, origin="operator", kind="text", payload="fix it")
    assert ledger.read(_TERMINAL) == _DRAFT


@pytest.mark.parametrize(
    ("disposition", "after_end", "after_later_input"),
    [
        ("completed", _EMPTY, _DRAFT),
        ("user_interrupted", _DRAFT, _DRAFT),
        ("ended_non_user", _DRAFT, _DRAFT),
        # The lifecycle keeps its wait on unknown evidence, so the dialog stays open.
        ("unknown", _EMPTY, _EMPTY),
    ],
)
def test_turn_end_closes_the_dialog_by_how_the_turn_ended(
    handlers: EventHandlers,
    ledger: ComposerLedger,
    disposition: TurnDisposition,
    after_end: LedgerRead,
    after_later_input: LedgerRead,
) -> None:
    handlers.handle_permission_request(_event(HookEventType.PERMISSION_REQUEST))
    ledger.observe_write(_TERMINAL, origin="operator", kind="key", payload="enter")
    stop = _event(HookEventType.STOP)
    stop.turn_disposition = disposition

    handlers.handle_stop(stop)

    assert ledger.read(_TERMINAL) == after_end
    ledger.observe_write(_TERMINAL, origin="operator", kind="text", payload="fix it")
    assert ledger.read(_TERMINAL) == after_later_input


def test_tool_work_with_no_dialog_open_skips_the_terminal_lookup(
    handlers: EventHandlers, ledger: ComposerLedger, terminals: MagicMock
) -> None:
    handlers.handle_before_tool(_event(HookEventType.BEFORE_TOOL, {"tool_name": "Bash"}))

    terminals.get_live_for_session.assert_not_called()
    assert ledger.read(_TERMINAL) == _EMPTY


def test_unbound_session_records_nothing(
    mock_dependencies: dict[str, Any], ledger: ComposerLedger
) -> None:
    ledger.observe_write(_TERMINAL, origin="operator", kind="text", payload="fix it", submit=True)
    terminals = MagicMock(name="terminal_manager")
    terminals.get_live_for_session.return_value = None
    handlers = EventHandlers(terminal_manager=terminals, **mock_dependencies)

    handlers.handle_before_agent(_event(HookEventType.BEFORE_AGENT, {"prompt": "fix it"}))

    assert ledger.read(_TERMINAL) == _DRAFT
