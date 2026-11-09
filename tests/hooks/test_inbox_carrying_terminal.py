"""A post-commit carrier mark failure leaves every carrying envelope terminal (#25851).

The acknowledgment commits before its carrying envelopes are terminalized. A failure in
between must leave the acknowledgment claimable, so a later sweep finishes those marks.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.hooks import inbox
from gobby.hooks.envelope_dedupe import (
    get_processed_envelope_dir,
    mark_envelope_processed,
    read_envelope_marker,
)
from gobby.storage.hook_receipts import prepare_receipt, release_receipt, reprepare_receipt
from gobby.storage.hub.protocol import HubDatabase
from tests.adapters.test_context_paging import _register_session

pytestmark = [pytest.mark.unit]

ORIGINAL = "carry-original"
CURRENT = "carry-current"
ACK = "carry-ack"


def _carried_receipt(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, machine: str) -> str:
    """A receipt carried by two envelopes: the original, then a re-prepared current one."""
    session_id = _register_session(temp_db, monkeypatch, machine)
    receipt = prepare_receipt(temp_db, session_id=session_id, envelope_id=ORIGINAL)
    assert release_receipt(temp_db, receipt_id=receipt.receipt_id, delivery_generation=1)
    carried = reprepare_receipt(temp_db, receipt_id=receipt.receipt_id, envelope_id=CURRENT)
    assert carried is not None
    return receipt.receipt_id


def _ack(inbox_dir: Path, receipt_id: str) -> Path:
    path = inbox_dir / f"{ACK}.json"
    body = {"kind": "delivery-receipt", "receipt_id": receipt_id, "delivery_generation": 2}
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _app(temp_db: HubDatabase) -> Any:
    return SimpleNamespace(state=SimpleNamespace(database=temp_db, hook_manager=None))


def _terminal(processed_dir: Path, envelope_id: str) -> bool:
    marker = read_envelope_marker(envelope_id, processed_dir=processed_dir)
    return marker is not None and marker.get("status", "processed") == "processed"


def _state(temp_db: HubDatabase, receipt_id: str) -> str:
    row = temp_db.fetchone(
        "SELECT state FROM hook_receipt_effects WHERE receipt_id = %s", (receipt_id,)
    )
    assert row is not None
    return str(row["state"])


def test_a_failed_carrier_mark_keeps_the_ack_claimable_until_every_carrier_is_terminal(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Per-envelope case: the original's marker write fails after the acknowledgment commits."""
    receipt_id = _carried_receipt(temp_db, monkeypatch, "22000000-0000-4000-8000-0000000000c1")
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    processed = get_processed_envelope_dir(inbox_dir)
    ack = _ack(inbox_dir, receipt_id)
    real_mark = mark_envelope_processed
    failures: list[str] = []

    def failing_mark(envelope_id: str, **kwargs: Any) -> None:
        if envelope_id == ORIGINAL and not failures:
            failures.append(envelope_id)
            raise OSError("marker write failed")
        real_mark(envelope_id, **kwargs)

    monkeypatch.setattr(inbox, "mark_envelope_processed", failing_mark)

    inbox.consume_pending_delivery_receipts(_app(temp_db), inbox_dir)
    assert _state(temp_db, receipt_id) == "acknowledged"
    assert ack.exists()
    assert not _terminal(processed, ORIGINAL)

    inbox.consume_pending_delivery_receipts(_app(temp_db), inbox_dir)
    assert not ack.exists()
    assert [_terminal(processed, e) for e in (ORIGINAL, CURRENT, ACK)] == [True, True, True]


def test_a_carrier_sweep_that_raises_keeps_the_ack_claimable_until_every_carrier_is_terminal(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Whole-helper case: the carrier sweep raises after the acknowledgment commits."""
    receipt_id = _carried_receipt(temp_db, monkeypatch, "22000000-0000-4000-8000-0000000000c2")
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    processed = get_processed_envelope_dir(inbox_dir)
    ack = _ack(inbox_dir, receipt_id)
    real_sweep = inbox._mark_carrying_envelopes_processed
    failures: list[str] = []

    def failing_sweep(*args: Any, **kwargs: Any) -> None:
        if not failures:
            failures.append("raised")
            raise RuntimeError("carrier sweep failed")
        real_sweep(*args, **kwargs)

    monkeypatch.setattr(inbox, "_mark_carrying_envelopes_processed", failing_sweep)

    inbox.consume_pending_delivery_receipts(_app(temp_db), inbox_dir)
    assert _state(temp_db, receipt_id) == "acknowledged"
    assert ack.exists()
    assert not _terminal(processed, ORIGINAL)
    assert not _terminal(processed, CURRENT)

    inbox.consume_pending_delivery_receipts(_app(temp_db), inbox_dir)
    assert not ack.exists()
    assert [_terminal(processed, e) for e in (ORIGINAL, CURRENT, ACK)] == [True, True, True]
