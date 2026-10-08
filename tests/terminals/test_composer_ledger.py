"""Composer ledger: composer state from input provenance, never from the screen."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gobby.terminals.composer_ledger import (
    ComposerLedger,
    LedgerRead,
    WaitOutcome,
    load_ledger,
    persist_ledger,
    write_ledger,
)
from gobby.terminals.host_events import InputActivityEvent, InterruptKind

pytestmark = pytest.mark.unit

_EMPTY = LedgerRead("empty")
_DRAFT = LedgerRead("draft")
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


def test_submit_flagged_host_input_is_consumed_by_the_next_record() -> None:
    ledger = _tracked()
    ledger.observe_host_input(_host(1))
    ledger.observe_host_input(_host(2, submit=True))

    assert ledger.read("t1") == _DRAFT

    ledger.record_submit("t1")
    assert ledger.read("t1") == _EMPTY


def test_input_after_the_flagged_chunk_stays_a_draft_after_the_record() -> None:
    ledger = _tracked()
    ledger.observe_host_input(_host(1, submit=True))
    ledger.observe_host_input(_host(2))
    ledger.record_submit("t1")

    assert ledger.read("t1") == _DRAFT


def test_record_without_a_new_flagged_write_does_not_move_the_clean_point() -> None:
    ledger = _tracked()
    ledger.observe_host_input(_host(1))
    ledger.record_submit("t1")

    assert ledger.read("t1") == _DRAFT


def test_old_host_input_without_submit_field_is_consumed_by_the_record() -> None:
    ledger = _tracked()
    ledger.observe_host_input(_host(1, submit=None))
    ledger.observe_host_input(_host(2, submit=None))
    ledger.record_submit("t1")

    assert ledger.read("t1") == _EMPTY


def test_interrupt_key_blocks_until_a_later_submit_is_recorded() -> None:
    ledger = _tracked()
    ledger.observe_host_input(_host(1, interrupt="esc"))

    assert ledger.read("t1") == LedgerRead("blocked", "interrupt")
    ledger.record_submit("t1")
    assert ledger.read("t1") == LedgerRead("blocked", "interrupt")

    ledger.observe_host_input(_host(2, submit=True))
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


def test_daemon_clear_keys_drop_held_text() -> None:
    ledger = _tracked()
    ledger.observe_write("t1", origin="automatic", kind="text", payload="continue")
    for key in ("ctrl_u", "ctrl_k", "backspace", "delete"):
        ledger.observe_write("t1", origin="automatic", kind="key", payload=key)

    assert ledger.read("t1") == _EMPTY


def test_daemon_interrupt_holds_unknown_content_without_blocking() -> None:
    ledger = _tracked()
    ledger.observe_write("t1", origin="daemon", kind="key", payload="escape")

    read = ledger.read("t1")
    assert read == LedgerRead("held", pending=None)
    assert not read.holds("/compact")


def test_human_draft_outranks_held_daemon_text() -> None:
    ledger = _tracked()
    ledger.observe_write("t1", origin="automatic", kind="text", payload="continue")
    ledger.observe_host_input(_host(1))

    assert ledger.read("t1") == _DRAFT


def test_dialog_input_is_consumed_when_a_clean_wait_resumes() -> None:
    ledger = _tracked()
    ledger.open_wait("t1")
    ledger.observe_host_input(_host(1, submit=True))
    ledger.close_wait("t1", "resumed")

    assert ledger.read("t1") == _EMPTY


@pytest.mark.parametrize("outcome", ["abandoned", "ambiguous"])
def test_dialog_input_becomes_a_draft_when_the_wait_is_not_resumed(outcome: WaitOutcome) -> None:
    ledger = _tracked()
    ledger.open_wait("t1")
    ledger.observe_host_input(_host(1))
    ledger.close_wait("t1", outcome)

    assert ledger.read("t1") == _DRAFT


def test_dialog_interrupt_blocks_when_the_wait_is_abandoned() -> None:
    ledger = _tracked()
    ledger.open_wait("t1")
    ledger.observe_host_input(_host(1, interrupt="esc"))
    ledger.close_wait("t1", "abandoned")

    assert ledger.read("t1") == LedgerRead("blocked", "interrupt")


def test_input_during_a_wait_opened_dirty_stays_human_input() -> None:
    ledger = _tracked()
    ledger.observe_host_input(_host(1))
    ledger.open_wait("t1")
    ledger.observe_host_input(_host(2, submit=True))
    ledger.close_wait("t1", "resumed")

    assert ledger.read("t1") == _DRAFT


def test_provider_limit_blocks_until_a_later_submit_or_release() -> None:
    ledger = _tracked("t1", "t2")
    ledger.block("t1", "provider_limit")
    ledger.block("t2", "provider_limit")
    ledger.record_submit("t1")

    assert ledger.read("t1") == LedgerRead("blocked", "provider_limit")

    ledger.observe_host_input(_host(1, submit=True))
    ledger.record_submit("t1")
    ledger.release("t2")
    assert ledger.read("t1") == _EMPTY
    assert ledger.read("t2") == _EMPTY


def test_release_clears_every_earlier_observation() -> None:
    ledger = _tracked()
    ledger.observe_host_input(_host(1, interrupt="ctrl_c"))
    ledger.observe_write("t1", origin="automatic", kind="text", payload="continue")
    ledger.release("t1")

    assert ledger.read("t1") == _EMPTY


def test_forget_returns_a_terminal_to_untracked() -> None:
    ledger = _tracked()
    ledger.forget("t1")

    assert ledger.read("t1") == _UNTRACKED


def test_host_break_blocks_every_tracked_terminal_and_sets_the_cursor() -> None:
    ledger = _tracked("t1", "t2")
    ledger.mark_host_break("gap", "e1", 40)

    assert ledger.read("t1") == LedgerRead("blocked", "gap")
    assert ledger.read("t2") == LedgerRead("blocked", "gap")
    assert ledger.host_cursor == ("e1", 40)

    ledger.observe_host_input(_host(41, submit=True))
    ledger.record_submit("t1")
    assert ledger.read("t1") == _EMPTY
    assert ledger.read("t2") == LedgerRead("blocked", "gap")


def test_host_epoch_change_blocks_every_tracked_terminal() -> None:
    ledger = _tracked("t1", "t2")
    ledger.observe_host_input(_host(5, terminal_id="t2", submit=True))
    ledger.observe_host_input(_host(1, terminal_id="t2", epoch="e2"))

    assert ledger.read("t1") == LedgerRead("blocked", "epoch")
    assert ledger.host_cursor == ("e2", 1)


def test_replayed_host_events_at_or_before_the_cursor_are_ignored() -> None:
    ledger = _tracked()
    ledger.observe_host_input(_host(7, submit=True))
    ledger.record_submit("t1")
    ledger.observe_host_input(_host(6))
    ledger.observe_host_input(_host(7))

    assert ledger.read("t1") == _EMPTY
    assert ledger.host_cursor == ("e1", 7)


def test_host_input_on_an_untracked_terminal_still_advances_the_cursor() -> None:
    ledger = ComposerLedger()
    ledger.observe_host_input(_host(3, terminal_id="other"))

    assert ledger.host_cursor == ("e1", 3)
    assert ledger.read("other") == _UNTRACKED


def test_state_round_trips_through_the_state_file(tmp_path: Path) -> None:
    ledger = _tracked("clean", "draft", "held", "limit")
    ledger.observe_host_input(_host(9, terminal_id="draft"))
    ledger.observe_write("held", origin="automatic", kind="text", payload="continue")
    ledger.block("limit", "provider_limit")
    ledger.open_wait("clean")
    path = tmp_path / "runtime" / "composer_ledger.json"

    write_ledger(ledger, path)
    restored = load_ledger(path)

    for terminal_id in ("clean", "draft", "held", "limit", "missing"):
        assert restored.read(terminal_id) == ledger.read(terminal_id)
    assert restored.host_cursor == ("e1", 9)
    restored.observe_host_input(_host(10, terminal_id="clean"))
    restored.close_wait("clean", "resumed")
    assert restored.read("clean") == _EMPTY
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_restored_ledger_continues_the_sequence(tmp_path: Path) -> None:
    ledger = _tracked()
    ledger.observe_host_input(_host(1, submit=True))
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
