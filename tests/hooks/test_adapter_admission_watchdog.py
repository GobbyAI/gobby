"""A wedged hook worker loses its session's admission slot instead of holding it forever (#24505)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.hooks import adapter_execution
from gobby.hooks.adapter_execution import (
    AdapterHookTimeout,
    abandon_wedged_admissions,
    run_adapter_hook,
)
from gobby.hooks.event_enrichment import EventEnricher
from gobby.hooks.events import HookEvent, HookResponse

pytestmark = pytest.mark.unit

_TIMEOUT = 0.2
_PAST_ABANDON_DEADLINE = 60.0
_ensure_admission_watchdog = adapter_execution._ensure_admission_watchdog


@pytest.fixture(autouse=True)
def _no_background_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each test drives the watchdog pass itself, at a chosen time. A watchdog thread
    # another test already started resolves the pass by module name, so it idles here.
    monkeypatch.setattr(adapter_execution, "_ensure_admission_watchdog", lambda: None)
    monkeypatch.setattr(adapter_execution, "abandon_wedged_admissions", lambda _now: 0)


@pytest.fixture
def gates() -> Iterator[dict[str, threading.Event]]:
    """Per-envelope gates a fake adapter blocks on; every gate opens at teardown."""
    opened: dict[str, threading.Event] = {}
    yield opened
    for gate in opened.values():
        gate.set()


@pytest.fixture
def finished(monkeypatch: pytest.MonkeyPatch) -> dict[str, threading.Event]:
    """Per-envelope events set once a worker's done callback has run in full."""
    events: dict[str, threading.Event] = {}
    end_abandoned_worker = adapter_execution._end_abandoned_worker

    def end_worker(
        admission: adapter_execution._SessionAdmission,
        holder: adapter_execution._AdmissionHolder,
    ) -> None:
        end_abandoned_worker(admission, holder)
        events[holder.envelope_id].set()

    monkeypatch.setattr(adapter_execution, "_end_abandoned_worker", end_worker)
    return events


def _adapter(gates: dict[str, threading.Event]) -> MagicMock:
    adapter = MagicMock()

    def handle_native(payload: dict[str, Any], _hook_manager: object) -> dict[str, Any]:
        envelope_id = payload["input_data"]["source_event_id"]
        gate = gates.get(envelope_id)
        if gate is not None:
            gate.wait(timeout=30)
        return {"continue": True, "envelope_id": envelope_id}

    adapter.handle_native.side_effect = handle_native
    return adapter


def _payload(session_id: str, envelope_id: str) -> dict[str, Any]:
    return {
        "_platform_session_id": session_id,
        "hook_type": "pre-tool-use",
        "source": "claude",
        "input_data": {"source_event_id": envelope_id, "tool_name": "Bash"},
    }


async def _run(
    adapter: MagicMock,
    finished: dict[str, threading.Event],
    session_id: str,
    envelope_id: str,
) -> dict[str, Any]:
    finished[envelope_id] = threading.Event()
    return await run_adapter_hook(
        adapter, _payload(session_id, envelope_id), MagicMock(), timeout_seconds=_TIMEOUT
    )


async def _wedge(
    adapter: MagicMock,
    gates: dict[str, threading.Event],
    finished: dict[str, threading.Event],
    session_id: str,
    envelope_id: str,
) -> None:
    gates[envelope_id] = threading.Event()
    with pytest.raises(AdapterHookTimeout) as raised:
        await _run(adapter, finished, session_id, envelope_id)
    assert raised.value.execution_duration_seconds is not None
    assert raised.value.execution_duration_seconds > 0


async def _release(
    gates: dict[str, threading.Event],
    finished: dict[str, threading.Event],
    envelope_id: str,
) -> None:
    gates[envelope_id].set()
    assert await asyncio.to_thread(finished[envelope_id].wait, 5)


@pytest.mark.asyncio
async def test_pending_message_is_injected_on_next_hook_after_abandoned_holder(
    gates: dict[str, threading.Event],
    finished: dict[str, threading.Event],
) -> None:
    session_id = "watchdog-inject-session"
    await _wedge(_adapter(gates), gates, finished, session_id, "env-wedged")
    assert abandon_wedged_admissions(time.monotonic() + _PAST_ABANDON_DEADLINE) == 1

    message = MagicMock()
    message.id = "msg-ruling"
    message.content = "Ruling: land the candidate"
    message.message_type = "message"
    message.from_session = "from-1111-2222-3333-444444444444"
    message.priority = "normal"
    message.metadata_json = None
    messages = MagicMock()
    messages.get_undelivered_messages.return_value = [message]
    enricher = EventEnricher(
        session_manager=None, injected_sessions=set(), inter_session_msg_manager=messages
    )

    def handle(event: HookEvent) -> HookResponse:
        response = HookResponse(decision="allow")
        enricher.enrich(event, response)
        return response

    hook_manager = MagicMock()
    hook_manager.handle.side_effect = handle
    payload = _payload(session_id, "env-after")
    payload["input_data"]["tool_input"] = {"command": "git status"}
    finished["env-after"] = threading.Event()
    output = await run_adapter_hook(
        ClaudeCodeAdapter(), payload, hook_manager, timeout_seconds=_TIMEOUT
    )

    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[Pending P2P messages from other sessions]:" in context
    assert "Ruling: land the candidate" in context
    messages.get_undelivered_messages.assert_called_once_with(session_id)
    assert await asyncio.to_thread(finished["env-after"].wait, 5)
    await _release(gates, finished, "env-wedged")
    assert session_id not in adapter_execution._session_admissions


@pytest.mark.asyncio
async def test_wedged_worker_is_abandoned_and_next_same_session_hook_runs(
    gates: dict[str, threading.Event],
    finished: dict[str, threading.Event],
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_id = "watchdog-abandon-session"
    adapter = _adapter(gates)
    await _wedge(adapter, gates, finished, session_id, "env-wedged")

    # The wedged worker still owns the slot, so the next hook never runs.
    with pytest.raises(AdapterHookTimeout) as queued:
        await _run(adapter, finished, session_id, "env-queued")
    assert queued.value.execution_duration_seconds == 0.0

    with caplog.at_level(logging.ERROR, logger=adapter_execution.__name__):
        abandoned = abandon_wedged_admissions(time.monotonic() + _PAST_ABANDON_DEADLINE)

    assert abandoned == 1
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.__dict__["session_id"] == session_id
    assert record.__dict__["envelope_id"] == "env-wedged"
    assert record.__dict__["hook_type"] == "pre-tool-use"
    assert record.__dict__["tool_name"] == "Bash"
    assert record.__dict__["elapsed_seconds"] >= _PAST_ABANDON_DEADLINE
    assert await _run(adapter, finished, session_id, "env-after") == {
        "continue": True,
        "envelope_id": "env-after",
    }

    # The abandoned worker exiting later releases nothing twice and frees its reference.
    await _release(gates, finished, "env-wedged")
    assert session_id not in adapter_execution._session_admissions
    assert await _run(adapter, finished, session_id, "env-final") == {
        "continue": True,
        "envelope_id": "env-final",
    }
    assert await asyncio.to_thread(finished["env-final"].wait, 5)
    assert session_id not in adapter_execution._session_admissions


@pytest.mark.asyncio
async def test_second_wedge_in_session_keeps_its_slot_while_first_still_runs(
    gates: dict[str, threading.Event],
    finished: dict[str, threading.Event],
) -> None:
    session_id = "watchdog-cap-session"
    adapter = _adapter(gates)
    await _wedge(adapter, gates, finished, session_id, "env-first")
    assert abandon_wedged_admissions(time.monotonic() + _PAST_ABANDON_DEADLINE) == 1

    await _wedge(adapter, gates, finished, session_id, "env-second")
    assert abandon_wedged_admissions(time.monotonic() + _PAST_ABANDON_DEADLINE) == 0
    with pytest.raises(AdapterHookTimeout):
        await _run(adapter, finished, session_id, "env-queued")

    # Once the first abandoned worker exits, the second one can be abandoned.
    await _release(gates, finished, "env-first")
    assert adapter_execution._session_admissions[session_id].abandoned == 0
    assert abandon_wedged_admissions(time.monotonic() + _PAST_ABANDON_DEADLINE) == 1

    await _release(gates, finished, "env-second")
    assert session_id not in adapter_execution._session_admissions


@pytest.mark.asyncio
async def test_worker_inside_its_deadline_is_not_abandoned(
    gates: dict[str, threading.Event],
    finished: dict[str, threading.Event],
) -> None:
    session_id = "watchdog-deadline-session"
    adapter = _adapter(gates)
    await _wedge(adapter, gates, finished, session_id, "env-slow")

    holder = adapter_execution._session_admissions[session_id].holder
    assert holder is not None
    deadline = adapter_execution.ADMISSION_ABANDON_TIMEOUTS * _TIMEOUT
    assert abandon_wedged_admissions(holder.admitted_at + deadline - 0.01) == 0
    assert adapter_execution._session_admissions[session_id].holder is holder

    await _release(gates, finished, "env-slow")
    assert session_id not in adapter_execution._session_admissions
    assert abandon_wedged_admissions(time.monotonic() + _PAST_ABANDON_DEADLINE) == 0


def test_watchdog_thread_starts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []

    class _Thread:
        def __init__(self, *, target: object, name: str, daemon: bool) -> None:
            assert daemon
            self.name = name

        def start(self) -> None:
            started.append(self.name)

    monkeypatch.setattr(adapter_execution, "_watchdog_started", False)
    monkeypatch.setattr(threading, "Thread", _Thread)

    _ensure_admission_watchdog()
    _ensure_admission_watchdog()

    assert started == ["gobby-hook-admission-watchdog"]
