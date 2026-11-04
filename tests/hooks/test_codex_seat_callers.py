"""Every caller that resolves a Codex seat honors a /clear successor's stored row (#25647).

A refused /clear successor's row records a settled origin and no seat. A fresh seat
TUI started in the cwd just before the thread was minted is what ordinary resolution
guesses for a new thread, so each caller below meets one beside the shared app-server
host. With the refusal stored, no caller may record that seat or leave it cached for
the thread's next hook. Without a stored /clear row the same caller guesses it, which
proves the caller resolves a seat at all.

The SessionStart flow's own enrichment (``_session_start/flow.py``) is covered end to
end in ``test_hooks_manager.py::test_codex_clear_stored_outcome_governs_every_later_hook``
(repeated /clear start: the /clear search; compact start: ordinary resolution).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch
from uuid import UUID

import psutil
import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.event_handlers._session_start.materialize import (
    activate_materialized_session,
    session_start_should_defer,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.session_lookup import SessionLookupService
from gobby.hooks.session_materialize import activate_deferred_session, build_synthetic_session_start
from gobby.hooks.terminal_context import clear_codex_seat_index, enrich_terminal_context_with_cwd
from gobby.storage.session_models import Session
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX

pytestmark = pytest.mark.unit

MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000025647"
MINTED_AT = 1_790_000_000.5
THREAD = str(UUID(int=(int(MINTED_AT * 1000) << 80) | (7 << 76) | (0x2 << 62) | 0x2564_7000_0001))
HOST_PID = 94728
NEIGHBOR_PID = 52002
HOST_CONTEXT: dict[str, Any] = {"parent_pid": HOST_PID, "gobby_terminal_id": "pane-host"}
STORED_ROWS: dict[str, dict[str, Any] | None] = {
    "refused": {"gobby_codex_clear_origin": "codex-tui", "cwd": "/repo"},
    "no-row": None,
}
EXPECTED_PID = {"refused": None, "no-row": NEIGHBOR_PID}
_MATERIALIZE = "gobby.hooks.event_handlers._session_start.materialize"
_LOOKUP = "gobby.hooks.session_lookup"
_DEFERRED = "gobby.hooks.session_materialize"


def _process(pid: int, cmdline: list[str], create_time: float) -> MagicMock:
    process = MagicMock()
    process.pid = pid
    process.info = {"name": "codex"}
    process.name.return_value = "codex"
    process.cmdline.return_value = cmdline
    process.create_time.return_value = create_time
    process.parent.return_value = None
    process.cwd.return_value = "/repo"
    process.terminal.return_value = None
    process.environ.return_value = {"GOBBY_TERMINAL_ID": f"pane-{pid}"}
    return process


@pytest.fixture(params=list(STORED_ROWS))
def stored(request: pytest.FixtureRequest) -> Iterator[str]:
    """The thread's stored row, a fresh neighbor seat, and no rescan throttle."""
    from gobby.hooks.terminal_context import configure_codex_seat_index

    processes = [
        _process(
            HOST_PID,
            ["codex", "app-server", "--listen", "unix://", "--managed-daemon"],
            50.0,
        ),
        _process(NEIGHBOR_PID, ["codex", "--yolo"], MINTED_AT - 30.0),
    ]

    def lookup(pid: int) -> MagicMock:
        for process in processes:
            if process.pid == pid:
                return process
        raise psutil.NoSuchProcess(pid)

    name = str(request.param)
    clear_codex_seat_index()
    configure_codex_seat_index({THREAD: STORED_ROWS[name]}.get)
    try:
        with (
            patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID),
            patch("gobby.hooks.terminal_context.psutil.Process", side_effect=lookup),
            patch(
                "gobby.hooks.terminal_context.psutil.process_iter",
                side_effect=lambda **_: list(processes),
            ),
            # Every hook rescans, so a throttle never hides a guess.
            patch("gobby.hooks.terminal_context._SEAT_RESCAN_INTERVAL_SECONDS", 0.0),
        ):
            yield name
    finally:
        configure_codex_seat_index(None)
        clear_codex_seat_index()


@contextmanager
def _enrichments(module: str) -> Iterator[list[tuple[str | None, dict[str, Any] | None]]]:
    """Record each seat resolution ``module`` asks for and the context it gets back."""
    recorded: list[tuple[str | None, dict[str, Any] | None]] = []

    def record(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        result = enrich_terminal_context_with_cwd(*args, **kwargs)
        recorded.append((kwargs.get("external_id"), result))
        return result

    with patch(f"{module}.enrich_terminal_context_with_cwd", side_effect=record):
        yield recorded


def _assert_seat(recorded: list[tuple[str | None, dict[str, Any] | None]], stored: str) -> None:
    """The caller resolved the thread's seat, and it and the next hook got the expected one."""
    assert [external_id for external_id, _context in recorded] == [THREAD] * len(recorded)
    assert recorded, "the caller resolves the thread's seat"
    expected = EXPECTED_PID[stored]
    assert [(context or {}).get("parent_pid") for _id, context in recorded] == [expected] * len(
        recorded
    )
    later = enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=THREAD)
    assert (later or {}).get("parent_pid") == expected


def _event(
    event_type: HookEventType,
    data: dict[str, Any] | None = None,
    *,
    source: SessionSource = SessionSource.CODEX,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=THREAD,
        source=source,
        timestamp=datetime.now(UTC),
        data={
            "cwd": "/repo",
            "transcript_path": "/repo/rollout.jsonl",
            "terminal_context": dict(HOST_CONTEXT),
            **(data or {}),
        },
        machine_id=MACHINE_ID,
        project_id="project-1",
        metadata={"_platform_session_id": "platform-1"},
    )


def _service(session_manager: MagicMock) -> SessionLookupService:
    return SessionLookupService(
        session_manager=session_manager,
        session_coordinator=MagicMock(),
        session_task_manager=MagicMock(),
        resolve_project_id=MagicMock(return_value="project-1"),
        logger=MagicMock(),
    )


def _row(stored: str) -> Session:
    context = STORED_ROWS[stored] or {"cwd": "/repo"}
    row = SimpleNamespace(
        id="platform-1",
        external_id=THREAD,
        machine_id=MACHINE_ID,
        project_id="project-1",
        terminal_context=dict(context),
        transcript_path="/repo/rollout.jsonl",
    )
    return cast(Session, row)


def test_startup_deferral_check(stored: str) -> None:
    """``_session_start/materialize.py``: whether a startup may defer its row."""
    event = _event(HookEventType.SESSION_START, {"source": "startup"})

    with _enrichments(_MATERIALIZE) as recorded:
        session_start_should_defer(event, None, "startup")

    _assert_seat(recorded, stored)


class _Bound(Exception):
    """Stops activation once it hands the resolved context to terminal binding."""


def test_session_start_activation(stored: str) -> None:
    """``_session_start/materialize.py``: activation resolving its own context."""
    event = _event(HookEventType.SESSION_START, {"source": "startup"})

    with (
        _enrichments(_MATERIALIZE) as recorded,
        patch(f"{_MATERIALIZE}.discover_and_bind_external_terminal", side_effect=_Bound),
        pytest.raises(_Bound),
    ):
        activate_materialized_session(
            MagicMock(),
            event,
            "platform-1",
            session_obj=_row(stored),
            project_id="project-1",
            transcript_path="/repo/rollout.jsonl",
        )

    _assert_seat(recorded, stored)


def test_inherited_session_hint(stored: str) -> None:
    """``session_lookup.py``: fencing an inherited platform-session hint."""
    event = _event(HookEventType.BEFORE_TOOL, source=SessionSource.GROK)
    parent = cast(
        Session,
        SimpleNamespace(id="parent-session", external_id="parent-thread", terminal_context={}),
    )

    with _enrichments(_LOOKUP) as recorded:
        _service(MagicMock())._accept_explicit_platform_session(event, parent)

    _assert_seat(recorded, stored)


@pytest.mark.parametrize("event_type", [HookEventType.BEFORE_AGENT, HookEventType.SESSION_END])
def test_terminal_context_backfill(stored: str, event_type: HookEventType) -> None:
    """``session_lookup.py``: the generic backfill on a known row's later hooks."""
    session_manager = MagicMock()
    session_manager.backfill_terminal_context.return_value = (None, False)

    with _enrichments(_LOOKUP) as recorded:
        _service(session_manager)._backfill_terminal_context(
            "platform-1", _event(event_type), _row(stored)
        )

    _assert_seat(recorded, stored)


def test_inherited_tty_native_subagent_bind(stored: str) -> None:
    """``session_lookup.py``: binding a child conversation to its pane's parent."""
    session_manager = MagicMock()
    session_manager.find_live_interactive_pane_owner.return_value = None

    with _enrichments(_LOOKUP) as recorded:
        _service(session_manager)._bind_inherited_tty_native_subagent(
            _event(HookEventType.BEFORE_TOOL), machine_id=MACHINE_ID
        )

    _assert_seat(recorded, stored)


def test_uncached_registration(stored: str) -> None:
    """``session_lookup.py``: registering a thread no row in this project matches."""
    session_manager = MagicMock()
    session_manager.get_session_id.return_value = None
    session_manager.lookup_session_id.return_value = None
    session_manager.recover_session.return_value = None
    session_manager.find_live_interactive_pane_owner.return_value = None
    session_manager.register_session.return_value = "platform-1"
    event = _event(HookEventType.BEFORE_TOOL)
    # No platform-session hint: only the thread id identifies the hook.
    event.metadata.clear()

    with _enrichments(_LOOKUP) as recorded:
        assert (
            _service(session_manager)._resolve_uncached_session_id(
                THREAD, event, machine_id=MACHINE_ID, project_id="project-1"
            )
            == "platform-1"
        )

    _assert_seat(recorded, stored)
    registered = session_manager.register_session.call_args.kwargs["terminal_context"]
    assert (registered or {}).get("parent_pid") == EXPECTED_PID[stored]


def test_synthetic_session_start(stored: str) -> None:
    """``session_materialize.py``: the SessionStart a deferred row is activated with."""
    with _enrichments(_DEFERRED) as recorded:
        build_synthetic_session_start(_event(HookEventType.BEFORE_AGENT), "platform-1")

    _assert_seat(recorded, stored)


def test_deferred_session_activation(stored: str) -> None:
    """``session_materialize.py``: activating a deferred row on its first activity."""
    manager = MagicMock()
    manager._session_manager.get.return_value = _row(stored)
    manager._session_manager.update.return_value = None
    manager._event_handlers._derive_transcript_path.return_value = "/repo/rollout.jsonl"
    manager._event_handlers._activate_materialized_session.return_value = []
    manager._event_handlers._compose_session_response.return_value = HookResponse(decision="allow")
    manager._evaluate_workflow_rules.return_value = (None, None)
    manager._evaluate_blocking_webhooks.return_value = None
    manager.get_machine_id.return_value = MACHINE_ID
    event = _event(HookEventType.AFTER_TOOL)
    event.metadata["_session_start_context_mode"] = "full"

    with (
        _enrichments(_DEFERRED) as recorded,
        patch(f"{_DEFERRED}.commit_stashed_startup_claim"),
    ):
        activate_deferred_session(manager, event, BlockingEffectDeadline(123.0))

    _assert_seat(recorded, stored)
    activated = manager._event_handlers._activate_materialized_session.call_args.kwargs
    assert (activated["terminal_context"] or {}).get("parent_pid") == EXPECTED_PID[stored]
