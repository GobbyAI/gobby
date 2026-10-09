"""The wake activity probe reads the composer ledger and the transcript, never the screen."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from gobby.agents.idle_detector import ComposerRead
from gobby.events.live_wake import TerminalActivity
from gobby.runner_init.wake_activity import TRANSCRIPT_TURN_OPEN, probe_terminal_activity
from gobby.terminals.composer_ledger import ComposerLedger

pytestmark = pytest.mark.unit

_USER_PROMPT = {"type": "user", "message": {"role": "user", "content": "run the tests"}}
_ASSISTANT = {"type": "assistant", "message": {"role": "assistant", "content": []}}
_TURN_DURATION = {"type": "system", "subtype": "turn_duration", "durationMs": 14085}


def _clean(ledger: ComposerLedger) -> None:
    ledger.record_spawn("term-1", "")


def _draft(ledger: ComposerLedger) -> None:
    _clean(ledger)
    ledger.observe_write("term-1", origin="operator", kind="text", payload="half-typed wor")


def _held(ledger: ComposerLedger) -> None:
    _clean(ledger)
    ledger.observe_write("term-1", origin="daemon", kind="text", payload="/compact")


def _blocked(ledger: ComposerLedger) -> None:
    _clean(ledger)
    ledger.block("term-1", "provider_limit")


def _untracked(ledger: ComposerLedger) -> None:
    del ledger


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "composer"),
    [
        (_clean, ComposerRead("empty")),
        (_draft, ComposerRead("draft")),
        (_held, ComposerRead("held", "/compact")),
        (_blocked, ComposerRead("unknown")),
        (_untracked, ComposerRead("unknown")),
    ],
    ids=["clean", "operator-draft", "daemon-text", "blocked", "untracked"],
)
async def test_composer_read_comes_from_the_ledger(
    composer_ledger: ComposerLedger,
    seed: Callable[[ComposerLedger], None],
    composer: ComposerRead,
) -> None:
    seed(composer_ledger)
    # No runtime, snapshot or pane is reachable from the probe's arguments.
    terminal = SimpleNamespace(id="term-1")
    session = SimpleNamespace(id="session-1", source="droid", transcript_path=None)

    assert await probe_terminal_activity(session, terminal) == TerminalActivity(composer)


@pytest.mark.asyncio
async def test_unbound_ledger_reads_unknown() -> None:
    terminal = SimpleNamespace(id="term-1")
    session = SimpleNamespace(id="session-1", source="droid", transcript_path=None)

    read = await probe_terminal_activity(session, terminal)

    assert read == TerminalActivity(ComposerRead("unknown"))


@pytest.mark.asyncio
async def test_raw_tmux_context_without_a_managed_terminal_reads_unknown(
    composer_ledger: ComposerLedger,
) -> None:
    composer_ledger.record_spawn("term-1", "")
    session = SimpleNamespace(
        id="session-1", source="claude", terminal_context={"tmux_pane": "%12"}
    )

    read = await probe_terminal_activity(session, None)

    assert read == TerminalActivity(ComposerRead("unknown"))


def _transcript(path: Path, *records: dict[str, object]) -> str:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return str(path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("records", "fingerprint"),
    [
        ((_TURN_DURATION, _USER_PROMPT), TRANSCRIPT_TURN_OPEN),
        ((_USER_PROMPT, _ASSISTANT), TRANSCRIPT_TURN_OPEN),
        ((_USER_PROMPT, _ASSISTANT, _TURN_DURATION), None),
        ((), None),
    ],
    ids=["prompt-submitted", "assistant-streaming", "turn-settled", "no-records"],
)
async def test_claude_transcript_decides_an_open_turn(
    composer_ledger: ComposerLedger,
    tmp_path: Path,
    records: tuple[dict[str, object], ...],
    fingerprint: str | None,
) -> None:
    composer_ledger.record_spawn("term-1", "")
    session = SimpleNamespace(
        id="session-1",
        source="claude",
        transcript_path=_transcript(tmp_path / "session.jsonl", *records),
    )

    read = await probe_terminal_activity(session, SimpleNamespace(id="term-1"))

    assert read == TerminalActivity(ComposerRead("empty"), turn_in_flight_fingerprint=fingerprint)


@pytest.mark.asyncio
async def test_missing_transcript_offers_no_in_flight_evidence(
    composer_ledger: ComposerLedger, tmp_path: Path
) -> None:
    composer_ledger.record_spawn("term-1", "")
    session = SimpleNamespace(
        id="session-1", source="claude", transcript_path=str(tmp_path / "gone.jsonl")
    )

    read = await probe_terminal_activity(session, SimpleNamespace(id="term-1"))

    assert read == TerminalActivity(ComposerRead("empty"))
