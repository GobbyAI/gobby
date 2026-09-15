"""Grok compact handoff: headless spawned runs are never typed into; TUI turns are
interrupted only after the live turn fails to settle.

A spawned Grok worker runs ``grok --single`` (headless): it reads nothing from its
terminal and its first Ctrl+C is a plain SIGINT that kills the run (#22364). The
staging tool refuses such runs outright. A Grok TUI session whose ``events.jsonl``
shows its last turn ended is compacted without an interrupt. A live turn is polled
until it settles or the wait expires; only a timeout interrupts with Ctrl+C before
``/compact`` is typed. Codex uses the same settle-then-submit path on its rollout.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.provider_capabilities import provider_capabilities
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._terminal import register_terminal_tools
from gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery import (
    deliver_staged_compact_handoff,
)
from gobby.terminals.composer import composer_clear_sequence
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
ATTEMPT_ID = "a" * 32
_DRAIN = composer_clear_sequence("grok")
_DELIVERY = "gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery"
_TMUX = "gobby.mcp_proxy.tools.sessions._terminal_tmux"
_SETTLE_WAIT = 0.3


class _TestRegistry(InternalToolRegistry):
    def get_tool(self, name: str) -> Callable[..., Any] | None:
        tool = self._tools.get(name)
        return tool.func if tool else None


class _GrokTurnPane:
    """Pane fake: Ctrl+C appends the CLI's interrupt record to the events file."""

    backend = "native"
    target = "term-grok"

    def __init__(
        self,
        events_path: Path,
        interrupt_record: dict[str, Any] | None = None,
    ) -> None:
        self.keys: list[str] = []
        self.typed: list[str] = []
        self.first_ctrl_c_at: float | None = None
        self._events_path = events_path
        self._interrupt_record = interrupt_record or {
            "type": "turn_ended",
            "outcome": "cancelled",
        }

    async def send_key(self, key: str) -> tuple[bool, str | None]:
        self.keys.append(key)
        if key == "ctrl_c":
            if self.first_ctrl_c_at is None:
                self.first_ctrl_c_at = time.monotonic()
            with self._events_path.open("ab") as stream:
                stream.write(json.dumps(self._interrupt_record).encode())
                stream.write(b"\n")
        return True, None

    async def type_text(self, text: str) -> tuple[bool, str | None]:
        self.typed.append(text)
        return True, None

    async def snapshot(self, lines: int = 12) -> str | None:
        return "ready\n> "


def _grok_events(tmp_path: Path, *records: dict[str, Any]) -> tuple[Path, Path]:
    transcript = tmp_path / "updates.jsonl"
    transcript.write_bytes(b"")
    events = tmp_path / "events.jsonl"
    events.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in records))
    return transcript, events


def _codex_rollout(tmp_path: Path, *payload_types: str) -> Path:
    transcript = tmp_path / "rollout.jsonl"
    records = [{"type": "event_msg", "payload": {"type": payload}} for payload in payload_types]
    transcript.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in records))
    return transcript


def _patch_settle_wait(monkeypatch: pytest.MonkeyPatch, wait: float = _SETTLE_WAIT) -> float:
    monkeypatch.setattr(f"{_TMUX}._TURN_SETTLE_WAIT_SECONDS", wait)
    monkeypatch.setattr(f"{_TMUX}._TURN_SETTLE_POLL_SECONDS", 0.05)
    return wait


async def _append_jsonl_later(path: Path, record: dict[str, Any], delay: float) -> None:
    await asyncio.sleep(delay)
    with path.open("ab") as stream:
        stream.write(json.dumps(record).encode() + b"\n")


async def _deliver(
    pane: _GrokTurnPane,
    transcript: Path,
    *,
    source: str = "grok",
) -> dict[str, Any]:
    session = SimpleNamespace(id=SESSION_ID, source=source, transcript_path=str(transcript))
    session_manager = MagicMock()
    session_manager.get.return_value = session
    with (
        patch(f"{_DELIVERY}._resolve_pane_io", return_value=(pane, None)),
        patch(f"{_DELIVERY}.mark_handoff_compact_continuation_pending", return_value=True),
        patch(f"{_DELIVERY}.clear_handoff_compact_continuation_pending", return_value=True),
        patch(f"{_DELIVERY}.clear_queued_context"),
        patch(f"{_DELIVERY}.record_handoff_delivery", return_value=True),
        patch(
            f"{_DELIVERY}.schedule_codex_handoff_compact_continuation_readiness",
            return_value=True,
        ),
    ):
        return await deliver_staged_compact_handoff(
            SESSION_ID,
            ATTEMPT_ID,
            "handoff-1",
            session_manager=session_manager,
            db=MagicMock(),
            agent_run_manager=MagicMock(),
        )


@pytest.mark.asyncio
async def test_settled_grok_turn_is_compacted_without_an_interrupt_key(tmp_path: Path) -> None:
    transcript, events = _grok_events(
        tmp_path,
        {"type": "turn_started", "turn_number": 4},
        {"type": "turn_ended", "outcome": "completed"},
    )
    pane = _GrokTurnPane(events)

    result = await _deliver(pane, transcript)

    assert result["compacted"] is True
    assert result["interrupted"] is False
    assert result["command"] == "/compact"
    assert pane.keys == [*_DRAIN, "enter"]
    assert pane.typed == ["/compact"]


@pytest.mark.asyncio
async def test_live_grok_turn_that_settles_is_compacted_without_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wait = _patch_settle_wait(monkeypatch)
    transcript, events = _grok_events(tmp_path, {"type": "turn_started", "turn_number": 4})
    pane = _GrokTurnPane(events)
    settler = asyncio.create_task(
        _append_jsonl_later(
            events,
            {"type": "turn_ended", "outcome": "completed"},
            min(0.08, wait / 3),
        )
    )

    result = await _deliver(pane, transcript)
    await settler

    assert result["compacted"] is True
    assert result["interrupted"] is False
    assert result["command"] == "/compact"
    assert pane.keys == [*_DRAIN, "enter"]
    assert "ctrl_c" not in pane.keys
    assert pane.typed == ["/compact"]


@pytest.mark.asyncio
async def test_live_grok_turn_is_interrupted_and_confirmed_before_compact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wait = _patch_settle_wait(monkeypatch)
    transcript, events = _grok_events(tmp_path, {"type": "turn_started", "turn_number": 4})
    pane = _GrokTurnPane(events)
    started = time.monotonic()

    result = await _deliver(pane, transcript)

    assert pane.first_ctrl_c_at is not None
    assert pane.first_ctrl_c_at - started >= wait
    assert result["compacted"] is True
    assert result["interrupted"] is True
    assert pane.keys == ["ctrl_c", *_DRAIN, "enter"]
    assert "escape" not in pane.keys
    assert pane.typed == ["/compact"]


@pytest.mark.asyncio
async def test_grok_mcp_tool_call_completed_without_turn_ended_waits_then_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wait = _patch_settle_wait(monkeypatch)
    transcript, events = _grok_events(
        tmp_path,
        {"type": "turn_started", "turn_number": 4},
        {"type": "mcp_tool_call_completed", "success": True},
    )
    pane = _GrokTurnPane(events)
    started = time.monotonic()

    result = await _deliver(pane, transcript)

    assert pane.first_ctrl_c_at is not None
    assert pane.first_ctrl_c_at - started >= wait
    assert result["compacted"] is True
    assert result["interrupted"] is True
    assert pane.keys[0] == "ctrl_c"
    assert pane.typed == ["/compact"]


@pytest.mark.asyncio
async def test_live_codex_turn_that_settles_is_compacted_without_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wait = _patch_settle_wait(monkeypatch)
    transcript = _codex_rollout(tmp_path, "task_started")
    pane = _GrokTurnPane(
        transcript,
        interrupt_record={"type": "event_msg", "payload": {"type": "turn_aborted"}},
    )
    settler = asyncio.create_task(
        _append_jsonl_later(
            transcript,
            {"type": "event_msg", "payload": {"type": "task_complete"}},
            min(0.08, wait / 3),
        )
    )

    result = await _deliver(pane, transcript, source="codex")
    await settler

    assert result["compacted"] is True
    assert result["interrupted"] is False
    assert result["command"] == "/compact"
    assert pane.keys == [*composer_clear_sequence("codex"), "enter"]
    assert "ctrl_c" not in pane.keys
    assert pane.typed == ["/compact"]


@pytest.mark.parametrize(
    ("provider", "headless"),
    [("grok", True), ("claude", False), ("codex", False), ("droid", False), ("qwen", False)],
)
def test_only_grok_spawns_headless(provider: str, headless: bool) -> None:
    assert provider_capabilities(provider).headless_spawn is headless


def _set_handoff_tool(
    agent_run_manager: MagicMock,
) -> tuple[Callable[..., Any], MagicMock]:
    registry = _TestRegistry(name="test", description="test")
    session = MagicMock(
        id=SESSION_ID,
        project_id="project-1",
        session_type="terminal",
        source="grok",
        status="active",
        terminal_context={"tmux_pane": "%12"},
        transcript_path=None,
    )
    session_manager = MagicMock()
    session_manager.get.return_value = session
    session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
    with patch(
        "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
        return_value=agent_run_manager,
    ):
        register_terminal_tools(
            registry, session_manager, MagicMock(fetchone=MagicMock(return_value=None))
        )
    set_handoff = registry.get_tool("set_handoff")
    assert set_handoff is not None
    return set_handoff, session_manager


def test_set_handoff_refuses_to_stage_delivery_for_a_headless_grok_run() -> None:
    agent_run_manager = MagicMock()
    agent_run_manager.get_by_session.return_value = SimpleNamespace(id="run-1", provider="grok")
    set_handoff, _session_manager = _set_handoff_tool(agent_run_manager)

    with (
        session_context_for_test(SESSION_ID),
        patch("gobby.mcp_proxy.tools.sessions._terminal._resolve_pane_io") as resolve_pane,
        patch("gobby.mcp_proxy.tools.sessions._terminal.stage_handoff_attempt") as stage,
    ):
        result = asyncio.run(set_handoff(current_state="Ready.", next_steps=["Continue."]))

    assert result["compacted"] is False
    assert result["error_code"] == "headless_agent_run"
    assert result.get("handoff_staged") is not True
    assert result.get("delivery_pending") is not True
    assert "Do not call set_handoff again" in result["retry_guidance"]
    agent_run_manager.get_by_session.assert_called_once_with(SESSION_ID)
    resolve_pane.assert_not_called()
    stage.assert_not_called()


def test_set_handoff_still_stages_delivery_for_a_tui_agent_run() -> None:
    agent_run_manager = MagicMock()
    agent_run_manager.get_by_session.return_value = SimpleNamespace(id="run-1", provider="codex")
    set_handoff, _session_manager = _set_handoff_tool(agent_run_manager)
    pane = MagicMock(backend="tmux", target="%12")
    pane.snapshot = MagicMock(return_value=_ready())

    with (
        session_context_for_test(SESSION_ID),
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal._resolve_pane_io",
            return_value=(pane, None),
        ),
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal._interrupt_observer",
            return_value=(None, None),
        ),
        patch("gobby.mcp_proxy.tools.sessions._terminal.stage_handoff_attempt"),
    ):
        result = asyncio.run(set_handoff(current_state="Ready.", next_steps=["Continue."]))

    assert result["handoff_staged"] is True
    assert result["delivery_pending"] is True


def _ready() -> Any:
    async def snapshot() -> str:
        return "ready"

    return snapshot()
