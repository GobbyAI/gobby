"""A receipt whose effects write another session's variables acknowledges under one lock (#25907).

Runs against the real hub database and the real SessionVariableManager, so the pool's
lock-order check is the one production uses.
"""

from __future__ import annotations

import pytest

from gobby.hooks.replay_decision import ReceiptSessionsChanged, acknowledge_and_apply_receipt
from gobby.storage.hook_receipts import HookReceipt, get_receipt, prepare_receipt
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.state_manager import SessionVariableManager
from tests.adapters.test_context_paging import _register_session

pytestmark = [pytest.mark.unit]

MACHINE = "21000000-0000-4000-8000-0000000259a7"
APPENDED = "receipt_lock_scope_marks"


def _cross_session_receipt(db: HubDatabase, owner: str, target: str) -> HookReceipt:
    return prepare_receipt(
        db,
        session_id=owner,
        envelope_id=f"lock-scope:{owner}",
        staged_payload={"session_id": target, "append_set_variables": {APPENDED: ["seen"]}},
    )


def test_effects_for_a_session_other_than_the_owner_commit_with_the_acknowledgment(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _register_session(temp_db, monkeypatch, MACHINE)
    target = _register_session(temp_db, monkeypatch, MACHINE)
    receipt = _cross_session_receipt(temp_db, owner, target)
    manager = SessionVariableManager(temp_db)

    committed = acknowledge_and_apply_receipt(
        temp_db,
        receipt_id=receipt.receipt_id,
        delivery_generation=receipt.delivery_generation,
        message_manager=None,
        variable_manager=manager,
    )

    assert committed is not None
    assert committed.state == "acknowledged"
    assert manager.get_variables(target)[APPENDED] == ["seen"]
    assert APPENDED not in manager.get_variables(owner)


def test_a_payload_restaged_onto_an_unlocked_session_rolls_the_acknowledgment_back(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _register_session(temp_db, monkeypatch, MACHINE)
    target = _register_session(temp_db, monkeypatch, MACHINE)
    receipt = _cross_session_receipt(temp_db, owner, target)
    manager = SessionVariableManager(temp_db)
    # The pre-CAS read sees an older payload that names only the owner.
    stale = {"session_id": owner, "staged_payload": {"session_id": owner}}
    real_fetchone = temp_db.fetchone

    def fetchone(sql: str, params: tuple[object, ...] = ()) -> object:
        if "staged_payload FROM hook_receipt_effects" in sql:
            return stale
        return real_fetchone(sql, params)

    monkeypatch.setattr(temp_db, "fetchone", fetchone)

    with pytest.raises(ReceiptSessionsChanged):
        acknowledge_and_apply_receipt(
            temp_db,
            receipt_id=receipt.receipt_id,
            delivery_generation=receipt.delivery_generation,
            message_manager=None,
            variable_manager=manager,
        )

    monkeypatch.undo()
    reread = get_receipt(temp_db, receipt_id=receipt.receipt_id)
    assert reread is not None
    assert reread.state == "prepared"
    assert APPENDED not in manager.get_variables(target)
