"""Receipt-ack application of staged one-shot session variables."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from gobby.hooks.receipt_effects import apply_acknowledged_receipt

SESSION_ID = "11111111-1111-4111-8111-111111111111"


class _VariableStore:
    def __init__(self) -> None:
        self.variables: dict[str, dict[str, Any]] = {}

    def merge_variables(self, session_id: str, updates: dict[str, Any]) -> bool:
        self.variables.setdefault(session_id, {}).update(updates)
        return True


class _MessageStore:
    def __init__(self) -> None:
        self.delivered: list[tuple[list[str], str]] = []

    def mark_delivered_batch(self, message_ids: list[str], session_id: str) -> None:
        self.delivered.append((list(message_ids), session_id))


def test_apply_acknowledged_receipt_merges_session_variables() -> None:
    variable_manager = _VariableStore()
    receipt = SimpleNamespace(
        receipt_id="receipt-1",
        session_id=SESSION_ID,
        staged_payload={
            "session_id": SESSION_ID,
            "session_variables": {"one_shot_guard": True},
        },
    )

    apply_acknowledged_receipt(receipt, variable_manager=variable_manager)

    assert variable_manager.variables[SESSION_ID]["one_shot_guard"] is True


def test_apply_acknowledged_receipt_skips_session_variables_without_manager() -> None:
    receipt = SimpleNamespace(
        receipt_id="receipt-1",
        session_id=SESSION_ID,
        staged_payload={"session_variables": {"one_shot_guard": True}},
    )

    apply_acknowledged_receipt(receipt)

    # No manager means the staged mutation stays uncommitted.
    assert receipt.staged_payload["session_variables"] == {"one_shot_guard": True}


def test_apply_acknowledged_receipt_merges_variables_and_marks_messages() -> None:
    variable_manager = _VariableStore()
    message_manager = _MessageStore()
    receipt = SimpleNamespace(
        receipt_id="receipt-1",
        session_id=SESSION_ID,
        staged_payload={
            "session_id": SESSION_ID,
            "session_variables": {"one_shot_guard": True},
            "pending_message_ids": ["msg-1"],
            "pending_message_session_id": SESSION_ID,
        },
    )

    apply_acknowledged_receipt(
        receipt,
        message_manager=message_manager,
        variable_manager=variable_manager,
    )

    assert variable_manager.variables[SESSION_ID]["one_shot_guard"] is True
    assert message_manager.delivered == [(["msg-1"], SESSION_ID)]
