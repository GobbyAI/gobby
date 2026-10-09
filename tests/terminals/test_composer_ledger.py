"""Composer ledger: composer state from input provenance, never from the screen."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gobby.agents.idle_detector import ComposerRead
from gobby.storage.terminals import Terminal
from gobby.terminals.composer import composer_clear_sequence
from gobby.terminals.composer_ledger import (
    ComposerLedger,
    ComposerReleaseError,
    LedgerRead,
    WaitOutcome,
    composer_drain_keys,
    load_ledger,
    persist_ledger,
    read_composer,
    record_composer_submit,
    release_composer,
    write_ledger,
)
from gobby.terminals.host_events import InputActivityEvent, InterruptKind, TerminalExitedEvent
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.unit

_EMPTY = LedgerRead("empty")
_DRAFT = LedgerRead("draft")
_GAP = LedgerRead("blocked", "gap")
_UNTRACKED = LedgerRead("blocked", "untracked")


def _host(
    seq: int,
    *,
    terminal_id: str = "t1",
    submit: bool | None = False,
    interrupt: InterruptKind | None = None,
    epoch: str = "e1",
) -> InputActivityEvent:
    return InputActivityEvent(
        terminal_id=terminal_id,
        host_terminal_id="h1",
        attachment_id="a1",
        kind="input",
        bytes=1,
        interrupt=interrupt,
        epoch=epoch,
        seq=seq,
        submit=submit,
    )


def _tracked(*terminal_ids: str) -> ComposerLedger:
    ledger = ComposerLedger()
    for terminal_id in terminal_ids or ("t1",):
        ledger.release(terminal_id)
    return ledger


def test_untracked_terminal_is_blocked_until_released() -> None:
    ledger = ComposerLedger()

    assert ledger.read("t1") == _UNTRACKED

    ledger.release("t1")
    assert ledger.read("t1") == _EMPTY


def test_submit_record_adopts_a_terminal_bound_before_the_ledger_tracked_it() -> None:
    ledger = ComposerLedger()
    ledger.resume_host("e1", 40, since=None, gap=False)
    ledger.observe_host_event(_host(41, submit=None))
    assert ledger.read("t1") == _UNTRACKED

    ledger.record_submit("t1")
    assert ledger.read("t1") == _EMPTY

    ledger.observe_host_event(_host(42))
    assert ledger.read("t1") == _DRAFT


def test_submit_flagged_host_input_is_consumed_by_the_next_record() -> None:
    ledger = _tracked()
    ledger.observe_host_event(_host(1))
    ledger.observe_host_event(_host(2, submit=True))

    assert ledger.read("t1") == _DRAFT

    ledger.record_submit("t1")
    assert ledger.read("t1") == _EMPTY


def test_input_after_the_flagged_chunk_stays_a_draft_after_the_record() -> None:
    ledger = _tracked()
    ledger.observe_host_event(_host(1, submit=True))
    ledger.observe_host_event(_host(2))
    ledger.record_submit("t1")

    assert ledger.read("t1") == _DRAFT


def test_record_without_a_new_flagged_write_does_not_move_the_clean_point() -> None:
    ledger = _tracked()
    ledger.observe_host_event(_host(1))
    ledger.record_submit("t1")

    assert ledger.read("t1") == _DRAFT


def test_old_host_input_without_submit_field_is_consumed_by_the_record() -> None:
    ledger = _tracked()
    ledger.observe_host_event(_host(1, submit=None))
    ledger.observe_host_event(_host(2, submit=None))
    ledger.record_submit("t1")

    assert ledger.read("t1") == _EMPTY


def test_interrupt_key_blocks_until_a_later_submit_is_recorded() -> None:
    ledger = _tracked()
    ledger.observe_host_event(_host(1, interrupt="esc"))

    assert ledger.read("t1") == LedgerRead("blocked", "interrupt")
    ledger.record_submit("t1")
    assert ledger.read("t1") == LedgerRead("blocked", "interrupt")

    ledger.observe_host_event(_host(2, submit=True))
    ledger.record_submit("t1")
    assert ledger.read("t1") == _EMPTY


def test_operator_writes_are_human_input_and_enter_flags_their_submit() -> None:
    ledger = _tracked()
    ledger.observe_write("t1", origin="operator", kind="text", payload="hello")

    assert ledger.read("t1") == _DRAFT

    ledger.observe_write("t1", origin="attention", kind="key", payload="enter")
    ledger.record_submit("t1")
    assert ledger.read("t1") == _EMPTY


def test_operator_raw_input_with_carriage_return_flags_submit() -> None:
    ledger = _tracked()
    ledger.observe_write("t1", origin="operator", kind="input", payload="ls\r")
    ledger.record_submit("t1")

    assert ledger.read("t1") == _EMPTY


def test_operator_escape_reads_as_an_interrupt() -> None:
    ledger = _tracked()
    ledger.observe_write("t1", origin="operator", kind="key", payload="escape")

    assert ledger.read("t1") == LedgerRead("blocked", "interrupt")


def test_daemon_text_is_held_until_its_submit_is_recorded() -> None:
    ledger = _tracked()
    ledger.observe_write("t1", origin="automatic", kind="text", payload="continue")

    read = ledger.read("t1")
    assert read == LedgerRead("held", pending="continue")
    assert read.holds(" continue ")
    assert not read.holds("/compact")

    ledger.observe_write("t1", origin="automatic", kind="key", payload="enter")
    ledger.record_submit("t1")
    assert ledger.read("t1") == _EMPTY


def test_daemon_text_written_with_submit_clears_on_the_record() -> None:
    ledger = _tracked()
    ledger.observe_write("t1", origin="daemon", kind="text", payload="/compact", submit=True)
    ledger.record_submit("t1")

    assert ledger.read("t1") == _EMPTY


def test_consecutive_daemon_text_writes_accumulate() -> None:
    ledger = _tracked()
    ledger.observe_write("t1", origin="daemon", kind="text", payload="/com")
    ledger.observe_write("t1", origin="daemon", kind="paste", payload="pact")

    assert ledger.read("t1") == LedgerRead("held", pending="/compact")


def test_daemon_clear_keys_hold_unknown_text_until_a_drain_is_recorded() -> None:
    # One Codex backspace removes one character, so no clear key proves the text gone.
    ledger = _tracked()
    ledger.observe_write("t1", origin="automatic", kind="text", payload="continue")
    for key in ("ctrl_u", "ctrl_k", "backspace", "delete"):
        ledger.observe_write("t1", origin="automatic", kind="key", payload=key)

    assert ledger.read("t1") == LedgerRead("held", pending=None)
    ledger.record_drain("t1")
    assert ledger.read("t1") == _EMPTY


def test_a_recorded_drain_keeps_a_human_draft() -> None:
    ledger = _tracked()
    ledger.observe_write("t1", origin="operator", kind="text", payload="private note")
    ledger.record_drain("t1")

    assert ledger.read("t1") == _DRAFT


def test_drain_keys_lead_with_one_backspace_per_held_character(
    composer_ledger: ComposerLedger,
) -> None:
    composer_ledger.record_spawn("t1", "")
    assert composer_drain_keys("t1", "codex") == composer_clear_sequence("codex")
    composer_ledger.observe_write("t1", origin="daemon", kind="text", payload="/compact\n")

    assert composer_drain_keys("t1", "codex") == (
        *("backspace",) * len("/compact\n"),
        *composer_clear_sequence("codex"),
    )
    composer_ledger.observe_write("t1", origin="daemon", kind="key", payload="escape")
    assert composer_drain_keys("t1", "codex") == composer_clear_sequence("codex")


def test_daemon_interrupt_holds_unknown_content_without_blocking() -> None:
    ledger = _tracked()
    ledger.observe_write("t1", origin="daemon", kind="key", payload="escape")

    read = ledger.read("t1")
    assert read == LedgerRead("held", pending=None)
    assert not read.holds("/compact")


def test_human_draft_outranks_held_daemon_text() -> None:
    ledger = _tracked()
    ledger.observe_write("t1", origin="automatic", kind="text", payload="continue")
    ledger.observe_host_event(_host(1))

    assert ledger.read("t1") == _DRAFT


def test_dialog_input_is_consumed_when_a_clean_wait_resumes() -> None:
    ledger = _tracked()
    ledger.open_wait("t1")
    ledger.observe_host_event(_host(1, submit=True))
    ledger.close_wait("t1", "resumed")

    assert ledger.read("t1") == _EMPTY


@pytest.mark.parametrize("outcome", ["abandoned", "ambiguous"])
def test_dialog_input_becomes_a_draft_when_the_wait_is_not_resumed(outcome: WaitOutcome) -> None:
    ledger = _tracked()
    ledger.open_wait("t1")
    ledger.observe_host_event(_host(1))
    ledger.close_wait("t1", outcome)

    assert ledger.read("t1") == _DRAFT


def test_dialog_interrupt_blocks_when_the_wait_is_abandoned() -> None:
    ledger = _tracked()
    ledger.open_wait("t1")
    ledger.observe_host_event(_host(1, interrupt="esc"))
    ledger.close_wait("t1", "abandoned")

    assert ledger.read("t1") == LedgerRead("blocked", "interrupt")


def test_submit_record_closes_a_wait_no_resolution_reached() -> None:
    ledger = _tracked()
    ledger.open_wait("t1")
    ledger.observe_host_event(_host(1, submit=True))
    ledger.record_submit("t1")

    assert ledger.read("t1") == _EMPTY
    assert not ledger.has_open_waits
    ledger.observe_host_event(_host(2))
    assert ledger.read("t1") == _DRAFT


def test_input_during_a_wait_opened_dirty_stays_human_input() -> None:
    ledger = _tracked()
    ledger.observe_host_event(_host(1))
    ledger.open_wait("t1")
    ledger.observe_host_event(_host(2, submit=True))
    ledger.close_wait("t1", "resumed")

    assert ledger.read("t1") == _DRAFT


def test_provider_limit_blocks_until_a_later_submit_or_release() -> None:
    ledger = _tracked("t1", "t2")
    ledger.block("t1", "provider_limit")
    ledger.block("t2", "provider_limit")
    ledger.record_submit("t1")

    assert ledger.read("t1") == LedgerRead("blocked", "provider_limit")

    ledger.observe_host_event(_host(1, submit=True))
    ledger.record_submit("t1")
    ledger.release("t2")
    assert ledger.read("t1") == _EMPTY
    assert ledger.read("t2") == _EMPTY


def test_release_clears_every_earlier_observation() -> None:
    ledger = _tracked()
    ledger.observe_host_event(_host(1, interrupt="ctrl_c"))
    ledger.observe_write("t1", origin="automatic", kind="text", payload="continue")
    ledger.release("t1")

    assert ledger.read("t1") == _EMPTY


def test_host_exit_event_returns_its_terminal_to_untracked() -> None:
    ledger = _tracked("t1", "t2")
    ledger.observe_host_event(
        TerminalExitedEvent(terminal_id="t1", host_terminal_id="h1", exit_code=0, epoch="e1", seq=4)
    )

    assert ledger.read("t1") == _UNTRACKED
    assert ledger.read("t2") == _EMPTY
    assert ledger.host_cursor == ("e1", 4)


def test_first_host_resume_blocks_every_tracked_terminal_until_a_later_submit() -> None:
    ledger = _tracked("t1", "t2")
    ledger.resume_host("e1", 40, since=None, gap=False)

    assert ledger.read("t1") == _GAP
    assert ledger.read("t2") == _GAP
    assert ledger.host_cursor == ("e1", 40)

    ledger.observe_host_event(_host(41, submit=True))
    ledger.record_submit("t1")
    assert ledger.read("t1") == _EMPTY
    assert ledger.read("t2") == _GAP


@pytest.mark.parametrize(
    ("since", "gap"),
    [(None, False), (7, False), (5, True)],
    ids=["no-replay", "replay-past-the-cursor", "host-gap"],
)
def test_resume_that_may_have_lost_input_blocks_every_tracked_terminal(
    since: int | None, gap: bool
) -> None:
    ledger = _tracked("t1", "t2")
    ledger.observe_host_event(_host(5, terminal_id="other"))
    ledger.resume_host("e1", 9, since=since, gap=gap)

    assert ledger.read("t1") == _GAP
    assert ledger.read("t2") == _GAP
    assert ledger.host_cursor == ("e1", 9)


@pytest.mark.parametrize(("since", "seq"), [(5, 9), (3, 9), (None, 5)])
def test_resume_that_replays_from_the_cursor_keeps_composer_state(
    since: int | None, seq: int
) -> None:
    ledger = _tracked()
    ledger.observe_host_event(_host(5, submit=True))
    ledger.resume_host("e1", seq, since=since, gap=False)

    assert ledger.read("t1") == _DRAFT
    assert ledger.host_cursor == ("e1", 5)
    ledger.observe_host_event(_host(6))
    assert ledger.host_cursor == ("e1", 6)


def test_resume_on_a_new_host_epoch_drops_every_tracked_terminal() -> None:
    ledger = _tracked("t1", "t2")
    ledger.observe_host_event(_host(5, terminal_id="t2", submit=True))
    ledger.resume_host("e2", 3, since=None, gap=False)

    assert ledger.read("t1") == _UNTRACKED
    assert ledger.read("t2") == _UNTRACKED
    assert ledger.host_cursor == ("e2", 3)


def test_host_event_from_another_epoch_is_ignored() -> None:
    ledger = _tracked("t1", "t2")
    ledger.observe_host_event(_host(5, terminal_id="t1"))
    ledger.observe_host_event(_host(6, terminal_id="t2", epoch="e2"))

    assert ledger.read("t1") == _DRAFT
    assert ledger.read("t2") == _EMPTY
    assert ledger.host_cursor == ("e1", 5)


def test_spawn_on_a_new_host_replays_that_host_from_its_first_event() -> None:
    ledger = _tracked()
    ledger.observe_host_event(_host(9))
    ledger.record_spawn("t2", "e2")

    assert ledger.read("t1") == _UNTRACKED
    assert ledger.read("t2") == _EMPTY
    assert ledger.host_cursor == ("e2", 0)
    # The reader resubscribes from the cursor and replays the spawn's input.
    ledger.resume_host("e2", 3, since=0, gap=False)
    ledger.observe_host_event(_host(1, terminal_id="t1", epoch="e1"))
    assert ledger.read("t2") == _EMPTY
    ledger.observe_host_event(_host(2, terminal_id="t2", epoch="e2"))
    assert ledger.read("t2") == _DRAFT
    assert ledger.host_cursor == ("e2", 2)


def test_spawn_on_a_new_host_blocks_when_the_resume_did_not_replay_its_start() -> None:
    ledger = _tracked()
    ledger.observe_host_event(_host(9))
    ledger.record_spawn("t2", "e2")
    ledger.resume_host("e2", 3, since=None, gap=False)

    assert ledger.read("t2") == _GAP


@pytest.mark.parametrize("epoch", ["e1", ""], ids=["cursor-host", "unknown-host"])
def test_spawn_without_a_new_host_keeps_tracked_terminals(epoch: str) -> None:
    ledger = _tracked()
    ledger.observe_host_event(_host(9))
    ledger.record_spawn("t2", epoch)

    assert ledger.read("t1") == _DRAFT
    assert ledger.read("t2") == _EMPTY
    assert ledger.host_cursor == ("e1", 9)


def test_replayed_host_events_at_or_before_the_cursor_are_ignored() -> None:
    ledger = _tracked()
    ledger.observe_host_event(_host(7, submit=True))
    ledger.record_submit("t1")
    ledger.observe_host_event(_host(6))
    ledger.observe_host_event(_host(7))

    assert ledger.read("t1") == _EMPTY
    assert ledger.host_cursor == ("e1", 7)


def test_host_input_on_an_untracked_terminal_still_advances_the_cursor() -> None:
    ledger = ComposerLedger()
    ledger.observe_host_event(_host(3, terminal_id="other"))

    assert ledger.host_cursor == ("e1", 3)
    assert ledger.read("other") == _UNTRACKED


def test_state_round_trips_through_the_state_file(tmp_path: Path) -> None:
    ledger = _tracked("clean", "draft", "held", "limit")
    ledger.observe_host_event(_host(9, terminal_id="draft"))
    ledger.observe_write("held", origin="automatic", kind="text", payload="continue")
    ledger.block("limit", "provider_limit")
    ledger.open_wait("clean")
    path = tmp_path / "runtime" / "composer_ledger.json"

    write_ledger(ledger, path)
    restored = load_ledger(path)

    for terminal_id in ("clean", "draft", "held", "limit", "missing"):
        assert restored.read(terminal_id) == ledger.read(terminal_id)
    assert restored.host_cursor == ("e1", 9)
    restored.observe_host_event(_host(10, terminal_id="clean"))
    restored.close_wait("clean", "resumed")
    assert restored.read("clean") == _EMPTY
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_restored_ledger_continues_the_sequence(tmp_path: Path) -> None:
    ledger = _tracked()
    ledger.observe_host_event(_host(1, submit=True))
    path = tmp_path / "ledger.json"
    write_ledger(ledger, path)

    restored = load_ledger(path)
    restored.record_submit("t1")

    assert restored.read("t1") == _EMPTY


@pytest.mark.parametrize(
    "content",
    ["{not json", '{"version": 99, "seq": 3, "host": null, "terminals": {}}', '{"version": 1}'],
)
def test_unreadable_state_file_restores_nothing(tmp_path: Path, content: str) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(content, encoding="utf-8")

    restored = load_ledger(path)

    assert restored.read("t1") == _UNTRACKED
    assert restored.host_cursor is None


def test_missing_state_file_restores_an_empty_ledger(tmp_path: Path) -> None:
    restored = load_ledger(tmp_path / "absent.json")

    assert restored.read("t1") == _UNTRACKED
    assert restored.host_cursor is None


def test_persist_loop_flushes_unsaved_changes_when_cancelled(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"

    async def run() -> None:
        ledger = ComposerLedger()
        task = asyncio.create_task(persist_ledger(ledger, path, interval=3600))
        await asyncio.sleep(0)
        ledger.release("t1")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert load_ledger(path).read("t1") == _EMPTY


def test_persist_loop_writes_nothing_without_changes(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"

    async def run() -> None:
        task = asyncio.create_task(persist_ledger(ComposerLedger(), path, interval=3600))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert not path.exists()


def _seat(session_id: str = "seat-session") -> tuple[Terminal, MemoryTerminalStore]:
    seat = make_memory_terminal(terminal_id="pre-land-seat")
    seat.session_id = session_id
    return seat, MemoryTerminalStore(seat)


def test_first_deploy_seat_reads_unknown_until_a_provider_submit_vouches_for_it(
    composer_ledger: ComposerLedger,
) -> None:
    seat, _terminals = _seat()

    assert read_composer(seat.id) == ComposerRead("unknown")
    record_composer_submit(seat.id)

    assert read_composer(seat.id) == ComposerRead("empty")


def test_first_deploy_seat_reads_unknown_until_an_operator_releases_it(
    composer_ledger: ComposerLedger,
) -> None:
    seat, terminals = _seat()

    assert read_composer(seat.id) == ComposerRead("unknown")
    released = release_composer(terminals, "seat-session", caller_session_id="assistant")

    assert released == seat.id
    assert read_composer(seat.id) == ComposerRead("empty")


@pytest.mark.parametrize(
    ("caller", "target", "code"),
    [
        ("seat-session", "seat-session", "self_release"),
        ("assistant", "terminal-less-session", "no_live_terminal"),
    ],
)
def test_release_refuses_a_self_call_and_a_session_without_a_terminal(
    composer_ledger: ComposerLedger, caller: str, target: str, code: str
) -> None:
    seat, terminals = _seat()

    with pytest.raises(ComposerReleaseError) as refused:
        release_composer(terminals, target, caller_session_id=caller)

    assert refused.value.code == code
    assert composer_ledger.read(seat.id) == _UNTRACKED


def test_release_without_a_running_ledger_is_refused() -> None:
    _seat_row, terminals = _seat()

    with pytest.raises(ComposerReleaseError) as refused:
        release_composer(terminals, "seat-session", caller_session_id=None)

    assert refused.value.code == "ledger_unavailable"
