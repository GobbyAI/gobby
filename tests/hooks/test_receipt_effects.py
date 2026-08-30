"""Receipt-ack application of staged one-shot session variables."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from gobby.hooks.receipt_effects import (
    apply_acknowledged_receipt,
    record_worker_staging,
    take_worker_staging,
)

SESSION_ID = "11111111-1111-4111-8111-111111111111"


class _VariableStore:
    def __init__(self) -> None:
        self.variables: dict[str, dict[str, Any]] = {}
        self.appended: list[tuple[str, str, list[str]]] = []

    def merge_variables(self, session_id: str, updates: dict[str, Any]) -> bool:
        self.variables.setdefault(session_id, {}).update(updates)
        return True

    def append_to_set_variable(
        self,
        session_id: str,
        name: str,
        values: list[str],
        *,
        preserve_order: bool = False,
    ) -> bool:
        del preserve_order
        self.appended.append((session_id, name, list(values)))
        existing = list(self.variables.setdefault(session_id, {}).get(name, []))
        for value in values:
            if value not in existing:
                existing.append(value)
        self.variables[session_id][name] = existing
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


def test_record_worker_staging_deep_merges_session_and_append_sets() -> None:
    take_worker_staging()
    record_worker_staging(
        {
            "session_id": SESSION_ID,
            "session_variables": {"one_shot_guard": True},
        }
    )
    record_worker_staging(
        {
            "session_id": SESSION_ID,
            "session_variables": {"_agent_context_injected": True},
            "append_set_variables": {"injected_memory_ids": ["a"]},
        }
    )
    record_worker_staging(
        {
            "append_set_variables": {"injected_memory_ids": ["a", "b"]},
            "pending_message_ids": ["msg-1"],
        }
    )

    staged = take_worker_staging()
    assert staged["session_id"] == SESSION_ID
    assert staged["session_variables"] == {
        "one_shot_guard": True,
        "_agent_context_injected": True,
    }
    assert staged["append_set_variables"]["injected_memory_ids"] == ["a", "b"]
    assert staged["pending_message_ids"] == ["msg-1"]


def test_apply_acknowledged_receipt_appends_set_variables_without_replacing() -> None:
    variable_manager = _VariableStore()
    variable_manager.variables[SESSION_ID] = {"injected_memory_ids": ["a"]}
    receipt = SimpleNamespace(
        receipt_id="receipt-append",
        session_id=SESSION_ID,
        staged_payload={
            "session_id": SESSION_ID,
            "append_set_variables": {"injected_memory_ids": ["b", "a"]},
        },
    )

    apply_acknowledged_receipt(receipt, variable_manager=variable_manager)

    assert variable_manager.appended == [(SESSION_ID, "injected_memory_ids", ["b", "a"])]
    assert variable_manager.variables[SESSION_ID]["injected_memory_ids"] == ["a", "b"]
    assert "one_shot_guard" not in variable_manager.variables[SESSION_ID]


def test_apply_acknowledged_receipt_duplicate_append_is_a_noop() -> None:
    variable_manager = _VariableStore()
    receipt = SimpleNamespace(
        receipt_id="receipt-dup",
        session_id=SESSION_ID,
        staged_payload={
            "session_id": SESSION_ID,
            "append_set_variables": {"suggested_skill_names": ["new-skill"]},
        },
    )

    apply_acknowledged_receipt(receipt, variable_manager=variable_manager)
    apply_acknowledged_receipt(receipt, variable_manager=variable_manager)

    assert variable_manager.variables[SESSION_ID]["suggested_skill_names"] == ["new-skill"]
    assert len(variable_manager.appended) == 2
    assert variable_manager.appended[0] == (SESSION_ID, "suggested_skill_names", ["new-skill"])


class _StartupStore(_VariableStore):
    def __init__(self) -> None:
        super().__init__()
        self.commits: list[tuple[str, int, str]] = []

    def commit_startup_context(
        self,
        session_id: str,
        generation: int,
        owner_token: str,
    ) -> bool:
        self.commits.append((session_id, generation, owner_token))
        return True


def test_apply_acknowledged_receipt_commits_startup_context() -> None:
    variable_manager = _StartupStore()
    receipt = SimpleNamespace(
        receipt_id="receipt-startup",
        session_id=SESSION_ID,
        staged_payload={
            "session_id": SESSION_ID,
            "startup_context": {
                "generation": 7,
                "owner_token": "owner-1",
                "session_id": SESSION_ID,
            },
        },
    )

    apply_acknowledged_receipt(receipt, variable_manager=variable_manager)

    assert variable_manager.commits == [(SESSION_ID, 7, "owner-1")]


def test_apply_acknowledged_receipt_skips_startup_commit_without_payload() -> None:
    variable_manager = _StartupStore()
    receipt = SimpleNamespace(
        receipt_id="receipt-no-startup",
        session_id=SESSION_ID,
        staged_payload={"session_id": SESSION_ID, "session_variables": {"k": True}},
    )

    apply_acknowledged_receipt(receipt, variable_manager=variable_manager)

    assert variable_manager.commits == []
    assert variable_manager.variables[SESSION_ID]["k"] is True
