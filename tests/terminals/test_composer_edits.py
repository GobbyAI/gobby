"""Reversible edits require an empty anchor and complete ordered input history."""

from typing import Any

import pytest

from gobby.terminals.composer_ledger import ComposerLedger, LedgerRead
from gobby.terminals.host_events import decode_host_event

pytestmark = pytest.mark.unit


def activity(ledger: ComposerLedger, seq: int, edits: object, *, submit: bool = False) -> None:
    ledger.observe_host_event(
        decode_host_event(
            {
                "event": "input_activity",
                "terminal_id": "t",
                "host_terminal_id": "h",
                "attachment_id": "a",
                "kind": "input",
                "bytes": 1,
                "interrupt": None,
                "epoch": "e",
                "seq": seq,
                "submit": submit,
                "effect": "edit",
                "edits": edits,
            }
        )
    )


def op(name: str, count: int = 1) -> dict[str, Any]:
    return {"op": name, "count": count}


def tracked() -> ComposerLedger:
    ledger = ComposerLedger()
    ledger.record_spawn("t", "e")
    return ledger


@pytest.mark.parametrize("native", [True, False])
def test_type_erase_returns_to_empty(native: bool) -> None:
    ledger = tracked()
    if native:
        activity(ledger, 1, [op("insert", 11)])
        activity(ledger, 2, [op("backspace", 10)])
    else:
        ledger.observe_write("t", origin="operator", kind="input", payload="draft-probe")
        ledger.observe_write("t", origin="operator", kind="input", payload="\x7f" * 10)
    assert ledger.read("t") == LedgerRead("draft")
    if native:
        activity(ledger, 3, [op("backspace")])
    else:
        ledger.observe_write("t", origin="operator", kind="key", payload="backspace")
    assert ledger.read("t") == LedgerRead("empty")


def test_backspace_on_empty_stays_empty() -> None:
    ledger = tracked()
    activity(ledger, 1, [op("backspace", 4)])
    assert ledger.read("t") == LedgerRead("empty")


@pytest.mark.parametrize("ack_first", [True, False])
def test_submit_preserves_later_edits_in_same_event(ack_first: bool) -> None:
    ledger = tracked()
    activity(ledger, 1, [op("insert", 2), op("submit"), op("insert", 3)], submit=True)
    if ack_first:
        ledger.record_submit("t")
    assert ledger.read("t") == LedgerRead("draft")
    activity(ledger, 2, [op("backspace", 3)])
    if not ack_first:
        assert ledger.read("t") == LedgerRead("draft")
        ledger.record_submit("t")
    assert ledger.read("t") == LedgerRead("empty")


@pytest.mark.parametrize(
    "edits",
    [
        None,
        [],
        [op("insert", -1)],
        [op("backspace", True)],
        [op("delete")],
        [op("insert", 0)],
        "bad",
        [op("insert"), {"op": "backspace"}],
    ],
)
def test_missing_or_malformed_summary_never_infers_empty(edits: object) -> None:
    ledger = tracked()
    activity(ledger, 1, edits)
    activity(ledger, 2, [op("backspace", 100)])
    assert ledger.read("t").state == "draft"


@pytest.mark.parametrize("payload", ["\x1b[A", "\x15", "é", "\x1b[200~abc\x1b[201~", "\t"])
def test_unsupported_edits_stay_unknown(payload: str) -> None:
    ledger = tracked()
    ledger.observe_write("t", origin="operator", kind="input", payload=payload)
    activity(ledger, 1, [op("backspace", 100)])
    assert ledger.read("t").state == "draft"


def test_gap_invalidates_known_edits() -> None:
    ledger = tracked()
    activity(ledger, 1, [op("insert", 2)])
    ledger.resume_host("e", 4, since=1, gap=True)
    activity(ledger, 5, [op("backspace", 2)])
    assert ledger.read("t") == LedgerRead("blocked", "gap")


def test_old_dirty_state_cannot_gain_an_edit_history() -> None:
    ledger = tracked()
    activity(ledger, 1, None)
    ledger = ComposerLedger.from_state(ledger.to_state())
    activity(ledger, 2, [op("backspace", 100)])
    assert ledger.read("t") == LedgerRead("draft")


def test_delayed_native_backspace_cannot_erase_a_later_daemon_draft() -> None:
    ledger = tracked()
    ledger.observe_write("t", origin="operator", kind="input", payload="later")
    # The native deletion reached the PTY first, but its host event arrived later.
    activity(ledger, 1, [op("backspace", 5)])
    assert ledger.read("t") == LedgerRead("draft")


def test_unconfirmed_daemon_backspace_cannot_clear_the_draft() -> None:
    ledger = tracked()
    ledger.observe_write("t", origin="operator", kind="input", payload="draft")
    ledger.observe_write("t", origin="operator", kind="input", payload="\x7f" * 5, confirmed=False)
    assert ledger.read("t") == LedgerRead("draft")


@pytest.mark.parametrize("intervening_input", [True, False])
def test_delivered_write_ack_preserves_intervening_input(intervening_input: bool) -> None:
    ledger = tracked()
    ledger.observe_write("t", origin="operator", kind="input", payload="draft")
    receipt = ledger.observe_write(
        "t", origin="operator", kind="input", payload="\x7f" * 5, confirmed=False
    )
    if intervening_input:
        activity(ledger, 1, [op("insert", 5)])
    ledger.confirm_write(receipt)
    assert ledger.read("t") == LedgerRead("draft" if intervening_input else "empty")


def test_automatic_text_prevents_empty_inference_until_drained() -> None:
    ledger = tracked()
    ledger.observe_write("t", origin="automatic", kind="text", payload="wake")
    activity(ledger, 1, [op("insert"), op("backspace")])
    assert ledger.read("t").state == "draft"


@pytest.mark.parametrize("summary", [None, [op("insert")], [{"op": [], "count": 1}]])
def test_bad_summary_cannot_acknowledge_a_same_chunk_draft(summary: object) -> None:
    ledger = tracked()
    activity(ledger, 1, summary, submit=True)
    ledger.record_submit("t")
    assert ledger.read("t") == LedgerRead("draft")
    activity(ledger, 2, [op("submit")], submit=True)
    ledger.record_submit("t")
    assert ledger.read("t") == LedgerRead("empty")
