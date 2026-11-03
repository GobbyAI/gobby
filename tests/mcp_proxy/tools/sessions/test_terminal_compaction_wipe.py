"""set_handoff wipes whatever the composer holds before it submits the command."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.idle_detector import IdleDetector
from gobby.mcp_proxy.tools.sessions._terminal_compaction import (
    _send_terminal_compaction_command,
)
from gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery import (
    deliver_staged_compact_handoff,
)
from gobby.terminals.composer_ledger import ComposerLedger
from gobby.terminals.pane_io import ComposerReader
from gobby.terminals.runtime import SnapshotMode
from tests.agents.detection_test_support import BundledDetectionRegistry

pytestmark = pytest.mark.unit

_TARGET = "term-wipe"
_DELIVERY = "gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery"
_COMPACTION = "gobby.mcp_proxy.tools.sessions._terminal_compaction"
_RULE = "─" * 40
_CLAUDE_READ = IdleDetector(BundledDetectionRegistry(), "claude").composer_read
_CODEX_READ = IdleDetector(BundledDetectionRegistry(), "codex").composer_read
_CONTINUATION = "Continue from your set_handoff: read get_handoff and resume the plan"


class _WipePane:
    """A composer that keys edit like the CLI does and that a submit empties.

    Claude kills the line with ctrl_u; Codex binds no line kill, so only one
    backspace per character empties it there. A submit into a non-empty
    composer appends to the leftover text instead of sending the command.
    """

    backend = "native"
    target = _TARGET

    def __init__(self, ledger: ComposerLedger, cli: str, composer: str) -> None:
        self.ledger = ledger
        self.cli = cli
        self.composer = composer
        self.keys: list[str] = []
        self.typed: list[str] = []
        self.submitted: list[str] = []

    async def send_key(self, key: str) -> tuple[bool, str | None]:
        self.keys.append(key)
        self.ledger.observe_write(self.target, origin="daemon", kind="key", payload=key)
        self._edit(key)
        return True, None

    def _edit(self, key: str) -> None:
        if key == "ctrl_u" and self.cli == "claude":
            self.composer = ""
        elif key == "backspace":
            self.composer = self.composer[:-1]

    async def type_text(self, text: str) -> tuple[bool, str | None]:
        self.typed.append(text)
        self.ledger.observe_write(self.target, origin="daemon", kind="text", payload=text)
        body = text.rstrip("\n")
        if self.composer:
            self.composer += body
        else:
            self.submitted.append(body)
            self.ledger.record_submit(self.target)
        return True, None

    async def snapshot(self, lines: int = 12, *, mode: SnapshotMode = "text") -> str:
        if self.cli == "codex":
            prompt = (
                f"› {self.composer}"
                if self.composer
                else "\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m"
            )
            return (
                f"done\n{prompt}\n\n"
                "\x1b[2m  GPT-6-Sol xhigh · ~/Projects/gobby · 0.5.0\n"
                "  ← for agents · ? for shortcuts\x1b[0m"
            )
        return "\n".join(["output", _RULE, f"❯ {self.composer}".rstrip(), _RULE, "  auto mode"])


class _StickyPane(_WipePane):
    """A composer no drain key edits."""

    def _edit(self, key: str) -> None:
        return None


def _operator_draft(ledger: ComposerLedger, text: str) -> None:
    ledger.observe_write(_TARGET, origin="operator", kind="text", payload=text)


def _ledger_empty(_ledger: ComposerLedger, _text: str) -> None:
    """Text the ledger never saw: typed before it tracked the seat."""


def _host_gap(ledger: ComposerLedger, _text: str) -> None:
    ledger.block(_TARGET, "gap")


async def _send(
    pane: _WipePane,
    composer_read: ComposerReader,
    *,
    turn_settled: Callable[[], bool] = lambda: True,
    observe_interrupt: Callable[[], bool | None] | None = None,
) -> tuple[bool, str | None, bool, dict[str, Any] | None]:
    return await _send_terminal_compaction_command(
        pane,
        "/compact",
        "session-wipe",
        cli_source=pane.cli,
        mark_continuation_pending=MagicMock(return_value=True),
        clear_continuation_pending=MagicMock(return_value=True),
        observe_interrupt=observe_interrupt,
        turn_settled=turn_settled,
        settle_seconds=0.0,
        composer_read=composer_read,
    )


_SEEDS = pytest.mark.parametrize(
    ("seed", "text"),
    [
        (_operator_draft, "half-typed operator draft"),
        (_ledger_empty, _CONTINUATION),
        (_host_gap, _CONTINUATION),
    ],
    ids=["operator-draft", "stuck-continuation-untracked", "stuck-continuation-host-gap"],
)
_CLIS = pytest.mark.parametrize(
    ("cli", "composer_read"),
    [("claude", _CLAUDE_READ), ("codex", _CODEX_READ)],
    ids=["claude", "codex"],
)


@pytest.mark.asyncio
@_SEEDS
@_CLIS
async def test_set_handoff_wipes_any_composer_text_then_compacts(
    composer_ledger: ComposerLedger,
    seed: Callable[[ComposerLedger, str], None],
    text: str,
    cli: str,
    composer_read: ComposerReader,
) -> None:
    composer_ledger.record_spawn(_TARGET, "")
    seed(composer_ledger, text)
    pane = _WipePane(composer_ledger, cli, text)

    result = await _send(pane, composer_read)

    assert result[0] is True, result
    assert pane.composer == ""
    assert pane.submitted == ["/compact"]
    assert pane.typed == ["/compact\n"]
    assert {"escape", "ctrl_c"}.isdisjoint(pane.keys)


@pytest.mark.asyncio
@_SEEDS
@_CLIS
@pytest.mark.parametrize("confirmed", [False, True], ids=["blind-interrupt", "confirmed-interrupt"])
async def test_set_handoff_during_a_live_turn_interrupts_wipes_then_compacts(
    composer_ledger: ComposerLedger,
    seed: Callable[[ComposerLedger, str], None],
    text: str,
    cli: str,
    composer_read: ComposerReader,
    confirmed: bool,
) -> None:
    """A turn that never settles is interrupted even with text in the composer."""
    composer_ledger.record_spawn(_TARGET, "")
    seed(composer_ledger, text)
    pane = _WipePane(composer_ledger, cli, text)

    result = await _send(
        pane,
        composer_read,
        turn_settled=lambda: False,
        observe_interrupt=(lambda: True) if confirmed else None,
    )

    assert result[0] is True, result
    assert pane.keys[0] == "escape"
    assert pane.composer == ""
    assert pane.submitted == ["/compact"]
    assert pane.typed == ["/compact\n"]


class _UnreadablePane(_WipePane):
    """A composer whose frame no reader can classify."""

    async def snapshot(self, lines: int = 12, *, mode: SnapshotMode = "text") -> str:
        return "  gobby | ~/Projects/gobby…\n✻ Crunching…\n  gobby | ~/Projects/gobby…"


@pytest.mark.asyncio
async def test_wipe_types_nothing_when_no_frame_proves_the_composer_empty(
    composer_ledger: ComposerLedger,
) -> None:
    """An unreadable frame never resets the ledger, so leftover text is never typed into."""
    composer_ledger.record_spawn(_TARGET, "")
    _operator_draft(composer_ledger, "operator draft")
    pane = _UnreadablePane(composer_ledger, "codex", "operator draft")

    result = await _send(pane, _CODEX_READ)

    assert result[0] is False
    assert result[3] is not None
    assert result[3]["error_code"] == "composer_not_clean"
    assert pane.typed == []


@pytest.mark.asyncio
async def test_wipe_fails_closed_when_text_survives_every_pass(
    composer_ledger: ComposerLedger,
) -> None:
    """A composer the keys cannot empty is never submitted into."""
    composer_ledger.record_spawn(_TARGET, "")
    _operator_draft(composer_ledger, "sticky")
    pane = _StickyPane(composer_ledger, "claude", "sticky")

    result = await _send(pane, _CLAUDE_READ)

    assert result[0] is False
    assert result[3] is not None
    assert result[3]["error_code"] == "composer_not_clean"
    assert pane.composer == "sticky"
    assert pane.typed == []


@pytest.mark.asyncio
async def test_staged_compact_handoff_wipes_an_operator_draft_and_compacts(
    composer_ledger: ComposerLedger,
) -> None:
    composer_ledger.record_spawn(_TARGET, "")
    _operator_draft(composer_ledger, "operator draft")
    pane = _WipePane(composer_ledger, "claude", "operator draft")
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(id="session-1", source="claude")

    with (
        patch(f"{_DELIVERY}._resolve_pane_io", return_value=(pane, None)),
        patch(f"{_DELIVERY}.recorded_seat_left", return_value=False),
        patch(f"{_DELIVERY}._interrupt_observer", return_value=(None, None)),
        patch(f"{_DELIVERY}._turn_settled_observer", return_value=lambda: True),
        patch(f"{_DELIVERY}.composer_reader", return_value=_CLAUDE_READ),
        patch(f"{_DELIVERY}.mark_handoff_compact_continuation_pending", return_value=True),
        patch(f"{_DELIVERY}.clear_handoff_compact_continuation_pending", return_value=True),
        patch(f"{_DELIVERY}._wait_for_compact_boundary", AsyncMock(return_value=None)),
        patch(f"{_DELIVERY}.clear_queued_context"),
        patch(f"{_COMPACTION}._SUBMIT_VERIFY_SETTLE_SECONDS", 0.0),
        patch(f"{_COMPACTION}._COMPACTION_CONFIRM_SETTLE_SECONDS", 0.0),
        patch(f"{_COMPACTION}._COMPACTION_REJECTION_SETTLE_SECONDS", 0.0),
    ):
        result = await deliver_staged_compact_handoff(
            "session-1",
            "a" * 32,
            "handoff-1",
            session_manager=session_manager,
            db=MagicMock(),
            agent_run_manager=MagicMock(),
        )

    # The boundary wait is stubbed, so no receipt is written; only a refusal sets a code.
    assert "error_code" not in result, result
    assert pane.composer == ""
    assert pane.submitted == ["/compact"]
