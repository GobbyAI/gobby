"""Receipt-effects storage authority tests."""

from __future__ import annotations

import importlib
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

pytestmark = pytest.mark.unit

_RECEIPT_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "crates/gcore/assets/schema/migrations/414_hook_receipt_effects.sql"
)


@pytest.fixture
def receipts_db(temp_db: HubDatabase) -> HubDatabase:
    sql = "\n".join(
        line
        for line in _RECEIPT_MIGRATION.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    )
    statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
    with temp_db.transaction() as conn:
        for statement in statements:
            conn.execute(statement)
    return temp_db


def _receipts() -> Any:
    spec = importlib.util.find_spec("gobby.storage.hook_receipts")
    assert spec is not None
    return importlib.import_module("gobby.storage.hook_receipts")


class TestHookReceiptConstants:
    def test_idempotency_window_is_a_fixed_timedelta(self) -> None:
        window = getattr(_receipts(), "HOOK_RECEIPT_IDEMPOTENCY_WINDOW", None)
        assert isinstance(window, timedelta)
        assert window.total_seconds() > 0


class TestHookReceiptLifecycle:
    def test_prepare_inserts_original_and_current_envelope_ids(
        self, receipts_db: HubDatabase
    ) -> None:
        receipts = _receipts()
        session_id = str(uuid4())
        envelope_id = "env-1"
        receipt = receipts.prepare_receipt(
            receipts_db,
            session_id=session_id,
            envelope_id=envelope_id,
            staged_payload={"context": "startup"},
        )
        assert receipt.original_envelope_id == envelope_id
        assert receipt.current_envelope_id == envelope_id
        assert receipt.delivery_generation == 1
        assert receipt.state == "prepared"
        assert receipt.session_id == session_id

    def test_acknowledge_commits_prepared_row(self, receipts_db: HubDatabase) -> None:
        receipts = _receipts()
        receipt = receipts.prepare_receipt(
            receipts_db,
            session_id=str(uuid4()),
            envelope_id="env-ack",
            staged_payload={"context": "startup"},
        )
        committed = receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=receipt.receipt_id,
            delivery_generation=receipt.delivery_generation,
        )
        assert committed is not None
        assert committed.state == "acknowledged"
        duplicate = receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=receipt.receipt_id,
            delivery_generation=receipt.delivery_generation,
        )
        assert duplicate is None or duplicate.state == "acknowledged"

    def test_release_and_reprepare_keeps_original_and_bumps_generation(
        self, receipts_db: HubDatabase
    ) -> None:
        receipts = _receipts()
        receipt = receipts.prepare_receipt(
            receipts_db,
            session_id=str(uuid4()),
            envelope_id="env-orig",
            staged_payload={"context": "startup"},
        )
        released = receipts.release_receipt(receipts_db, receipt_id=receipt.receipt_id)
        assert released is not None
        assert released.state == "released"
        reprepared = receipts.reprepare_receipt(
            receipts_db,
            receipt_id=receipt.receipt_id,
            envelope_id="env-next",
        )
        assert reprepared is not None
        assert reprepared.original_envelope_id == "env-orig"
        assert reprepared.current_envelope_id == "env-next"
        assert reprepared.delivery_generation == 2
        assert reprepared.state == "prepared"
        stale = receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=receipt.receipt_id,
            delivery_generation=1,
        )
        assert stale is None

    def test_missing_receipt_ack_is_a_noop(self, receipts_db: HubDatabase) -> None:
        receipts = _receipts()
        assert (
            receipts.acknowledge_receipt(
                receipts_db,
                receipt_id=str(uuid4()),
                delivery_generation=1,
            )
            is None
        )


class TestForceContinueBudget:
    def test_increment_serializes_and_does_not_refund(self, receipts_db: HubDatabase) -> None:
        receipts = _receipts()
        session_id = str(uuid4())
        first = receipts.increment_force_continue(receipts_db, session_id, execution_num=1)
        second = receipts.increment_force_continue(receipts_db, session_id, execution_num=1)
        assert first == 1
        assert second == 2
        other_exec = receipts.increment_force_continue(receipts_db, session_id, execution_num=2)
        assert other_exec == 1
        limit = getattr(receipts, "AGY_FORCE_CONTINUE_LIMIT", None)
        assert limit is None or isinstance(limit, int)


def _set_transition(
    db: HubDatabase,
    receipt_id: str,
    when: datetime,
    *,
    state: str | None = None,
) -> None:
    with db.transaction() as conn:
        if state is None:
            conn.execute(
                "UPDATE hook_receipt_effects SET transition_at = %s WHERE receipt_id = %s",
                (when, receipt_id),
            )
        else:
            conn.execute(
                "UPDATE hook_receipt_effects SET transition_at = %s, state = %s "
                "WHERE receipt_id = %s",
                (when, state, receipt_id),
            )


def _receipt_count(db: HubDatabase) -> int:
    with db.transaction() as conn:
        row = conn.execute("SELECT count(*) AS n FROM hook_receipt_effects").fetchone()
    assert row is not None
    return int(row["n"])


class TestHookReceiptRetention:
    def test_prune_constant_is_a_fixed_positive_int(self) -> None:
        receipts = _receipts()
        max_entries = getattr(receipts, "HOOK_RECEIPT_PRUNE_MAX_ENTRIES", None)
        assert isinstance(max_entries, int)
        assert max_entries > 0

    def test_prepared_and_released_rows_are_never_pruned(self, receipts_db: HubDatabase) -> None:
        receipts = _receipts()
        prune = getattr(receipts, "prune_hook_receipts", None)
        assert callable(prune)
        window = receipts.HOOK_RECEIPT_IDEMPOTENCY_WINDOW
        now = utc_now()
        prepared = receipts.prepare_receipt(
            receipts_db,
            session_id=str(uuid4()),
            envelope_id="env-prepared",
        )
        released = receipts.prepare_receipt(
            receipts_db,
            session_id=str(uuid4()),
            envelope_id="env-released",
        )
        receipts.release_receipt(receipts_db, receipt_id=released.receipt_id)
        stale = now - (window + timedelta(seconds=5))
        _set_transition(receipts_db, prepared.receipt_id, stale)
        _set_transition(receipts_db, released.receipt_id, stale, state="released")

        result = prune(receipts_db, now=now)
        assert result.deleted == 0
        assert _receipt_count(receipts_db) == 2

    def test_acknowledged_and_terminal_rows_prune_strictly_after_window(
        self, receipts_db: HubDatabase
    ) -> None:
        receipts = _receipts()
        prune = getattr(receipts, "prune_hook_receipts", None)
        assert callable(prune)
        window = receipts.HOOK_RECEIPT_IDEMPOTENCY_WINDOW
        now = utc_now()
        inside = receipts.prepare_receipt(
            receipts_db, session_id=str(uuid4()), envelope_id="env-inside"
        )
        exact = receipts.prepare_receipt(
            receipts_db, session_id=str(uuid4()), envelope_id="env-exact"
        )
        outside = receipts.prepare_receipt(
            receipts_db, session_id=str(uuid4()), envelope_id="env-outside"
        )
        receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=inside.receipt_id,
            delivery_generation=1,
        )
        receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=exact.receipt_id,
            delivery_generation=1,
        )
        receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=outside.receipt_id,
            delivery_generation=1,
        )
        _set_transition(receipts_db, inside.receipt_id, now - (window - timedelta(seconds=1)))
        _set_transition(receipts_db, exact.receipt_id, now - window)
        _set_transition(receipts_db, outside.receipt_id, now - (window + timedelta(seconds=1)))

        terminal = receipts.prepare_receipt(
            receipts_db, session_id=str(uuid4()), envelope_id="env-terminal"
        )
        receipts.terminalize_receipts_for_envelope(receipts_db, envelope_id="env-terminal")
        _set_transition(
            receipts_db,
            terminal.receipt_id,
            now - (window + timedelta(seconds=1)),
        )

        result = prune(receipts_db, now=now)
        assert result.deleted == 2
        assert _receipt_exists(receipts_db, inside.receipt_id)
        assert _receipt_exists(receipts_db, exact.receipt_id)
        assert not _receipt_exists(receipts_db, outside.receipt_id)
        assert not _receipt_exists(receipts_db, terminal.receipt_id)

    def test_duplicate_ack_after_prune_is_a_noop_and_writes_no_row(
        self, receipts_db: HubDatabase
    ) -> None:
        receipts = _receipts()
        prune = getattr(receipts, "prune_hook_receipts", None)
        assert callable(prune)
        window = receipts.HOOK_RECEIPT_IDEMPOTENCY_WINDOW
        now = utc_now()
        receipt = receipts.prepare_receipt(
            receipts_db, session_id=str(uuid4()), envelope_id="env-gone"
        )
        receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=receipt.receipt_id,
            delivery_generation=1,
        )
        _set_transition(
            receipts_db,
            receipt.receipt_id,
            now - (window + timedelta(seconds=2)),
        )
        prune(receipts_db, now=now)
        assert _receipt_count(receipts_db) == 0
        duplicate = receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=receipt.receipt_id,
            delivery_generation=1,
        )
        assert duplicate is None
        assert _receipt_count(receipts_db) == 0

    def test_prune_is_bounded(self, receipts_db: HubDatabase) -> None:
        receipts = _receipts()
        prune = getattr(receipts, "prune_hook_receipts", None)
        assert callable(prune)
        window = receipts.HOOK_RECEIPT_IDEMPOTENCY_WINDOW
        now = utc_now()
        stale = now - (window + timedelta(seconds=5))
        ids: list[str] = []
        for index in range(4):
            receipt = receipts.prepare_receipt(
                receipts_db,
                session_id=str(uuid4()),
                envelope_id=f"env-bound-{index}",
            )
            receipts.acknowledge_receipt(
                receipts_db,
                receipt_id=receipt.receipt_id,
                delivery_generation=1,
            )
            _set_transition(receipts_db, receipt.receipt_id, stale)
            ids.append(receipt.receipt_id)

        result = prune(receipts_db, now=now, max_entries=2)
        assert result.deleted == 2
        assert result.truncated is True
        assert _receipt_count(receipts_db) == 2


def _receipt_exists(db: HubDatabase, receipt_id: str) -> bool:
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT 1 FROM hook_receipt_effects WHERE receipt_id = %s",
            (receipt_id,),
        ).fetchone()
    return row is not None
