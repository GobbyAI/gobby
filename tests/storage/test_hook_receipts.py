"""Receipt-effects storage authority tests."""

from __future__ import annotations

import importlib
import importlib.util
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from gobby.storage.hub.protocol import HubDatabase

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
