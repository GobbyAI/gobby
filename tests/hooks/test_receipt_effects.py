"""Receipt-ack application of staged one-shot session variables."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

from gobby.hooks.receipt_effects import (
    apply_acknowledged_receipt,
    peek_worker_staging,
    record_worker_staging,
    take_worker_staging,
    worker_staging_scope,
)
from gobby.workflows.engine._offload import offload

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


OTHER_SESSION_ID = "22222222-2222-4222-8222-222222222222"


async def _stage_one_delivery(session_id: str, barrier: threading.Barrier) -> dict[str, Any]:
    """Stage from the runtime thread and from an offloaded worker thread."""

    def stage_from_offload() -> None:
        record_worker_staging(
            {
                "session_id": session_id,
                "append_set_variables": {"injected_memory_ids": [f"{session_id}-offloaded"]},
            }
        )

    # Rule effects stage from inside offload(), which copies the context onto a
    # shared rule-engine executor thread.
    await offload(stage_from_offload)
    record_worker_staging({"session_id": session_id, "session_variables": {session_id: True}})
    # Hold both deliveries here so each one peeks only after the other staged.
    await asyncio.to_thread(barrier.wait, 10)
    return peek_worker_staging()


async def _peek_shared_threads() -> dict[str, dict[str, Any]]:
    """What a fresh delivery finds on the runtime and executor threads."""

    return {"runtime": peek_worker_staging(), "offloaded": await offload(peek_worker_staging)}


def test_worker_staging_scope_isolates_concurrent_deliveries() -> None:
    """Deliveries sharing the runtime and executor threads never mix (#21427).

    Production topology: adapter worker threads submit rule evaluation to one
    long-lived "gobby-workflow-runtime" loop thread, which offloads blocking
    work to a shared rule-engine executor. A thread-local staging buffer let
    those shared threads carry one delivery's staged effects into the next.
    """
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(
        target=loop.run_forever, name="gobby-workflow-runtime-test", daemon=True
    )
    loop_thread.start()
    barrier = threading.Barrier(2)

    def deliver(session_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        with worker_staging_scope():
            runtime_view = asyncio.run_coroutine_threadsafe(
                _stage_one_delivery(session_id, barrier), loop
            ).result(timeout=30)
            return runtime_view, take_worker_staging()

    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="gobby-hook-adapter") as pool:
            first = pool.submit(deliver, SESSION_ID)
            second = pool.submit(deliver, OTHER_SESSION_ID)
            first_runtime, first_staged = first.result(timeout=30)
            second_runtime, second_staged = second.result(timeout=30)

        # What the runtime thread sees mid-evaluation is this delivery's staging
        # only — that view is what _apply_staged_effects_metadata copies onto the
        # hook response, and it used to carry the other session's variables.
        for session_id, runtime_view in (
            (SESSION_ID, first_runtime),
            (OTHER_SESSION_ID, second_runtime),
        ):
            assert runtime_view["session_id"] == session_id
            assert runtime_view["session_variables"] == {session_id: True}
            assert runtime_view["append_set_variables"] == {
                "injected_memory_ids": [f"{session_id}-offloaded"]
            }

        # The adapter thread drains exactly what its own delivery staged,
        # including the write made on the offloaded worker thread.
        assert first_staged == first_runtime
        assert second_staged == second_runtime

        # A later delivery on those same shared threads starts clean. Under the
        # thread-local buffer it inherited everything staged above.
        def probe() -> dict[str, dict[str, Any]]:
            with worker_staging_scope():
                return asyncio.run_coroutine_threadsafe(_peek_shared_threads(), loop).result(
                    timeout=30
                )

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="gobby-hook-adapter") as pool:
            assert pool.submit(probe).result(timeout=30) == {"runtime": {}, "offloaded": {}}
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=10)
        loop.close()


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
