"""A receipt's acknowledgment and its variable effects commit together or not at all (#25851).

Runs against the real hub database and the real SessionVariableManager. Only the paging
queue write is made to raise, once, so each failure lands on the write itself.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk.types import SyncHookJSONOutput

from gobby.adapters.degradation import CARRIED_CONTEXT_VARIABLE
from gobby.hooks.events import HookEventType, HookResponse
from gobby.hooks.receipt_effects import (
    STAGED_EFFECTS_FIELD,
    record_worker_staging,
    take_worker_staging,
    worker_staging_scope,
)
from gobby.hooks.replay_decision import acknowledge_and_apply_receipt, commit_staged_effects
from gobby.servers.websocket.chat import ChatMixin
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.state_manager import SessionVariableManager
from tests.adapters.test_context_paging import (
    _ChatHooks,
    _delivered_context,
    _owe_one_shot_context,
    _queue,
    _register_session,
)

pytestmark = [pytest.mark.unit]

BACKLOG = [["rule:common", "c" * 5_000]]
REMAINDER = [["rule:tail", "t" * 300]]


def _fail_queue_write_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the real merge raise once, on the paging queue key, and succeed afterwards."""
    real_merge = SessionVariableManager.merge_variables
    failed: list[bool] = []

    def flaky_merge(
        self: SessionVariableManager, session_id: str, updates: dict[str, Any], **kwargs: Any
    ) -> bool:
        if CARRIED_CONTEXT_VARIABLE in updates and not failed:
            failed.append(True)
            raise RuntimeError("queue write failed")
        return real_merge(self, session_id, updates, **kwargs)

    monkeypatch.setattr(SessionVariableManager, "merge_variables", flaky_merge)


def _staged(
    session_id: str, variables: dict[str, Any], appends: dict[str, list[str]] | None = None
) -> dict[str, Any]:
    """Stage one delivery's effects exactly as a hook output does, then take them."""
    with worker_staging_scope():
        payload: dict[str, Any] = {"session_id": session_id, "session_variables": variables}
        if appends:
            payload["append_set_variables"] = appends
        record_worker_staging(payload)
        return take_worker_staging()


def _receipts(db: HubDatabase, session_id: str) -> list[dict[str, Any]]:
    rows = db.fetchall(
        "SELECT receipt_id, state, delivery_generation FROM hook_receipt_effects "
        "WHERE session_id = %s ORDER BY created_at",
        (session_id,),
    )
    return [dict(row) for row in rows]


def test_a_queue_write_that_raises_keeps_the_receipt_prepared_and_the_backlog_unchanged(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-0000000000a1")
    manager = SessionVariableManager(temp_db)
    manager.merge_variables(session_id, {CARRIED_CONTEXT_VARIABLE: BACKLOG})
    _fail_queue_write_once(monkeypatch)
    staged = _staged(session_id, {CARRIED_CONTEXT_VARIABLE: REMAINDER})

    with pytest.raises(RuntimeError, match="queue write failed"):
        commit_staged_effects(
            temp_db, session_id=session_id, staged=staged, variable_manager=manager
        )

    assert [r["state"] for r in _receipts(temp_db, session_id)] == ["prepared"]
    assert _queue(temp_db, session_id) == BACKLOG

    commit_staged_effects(temp_db, session_id=session_id, staged=staged, variable_manager=manager)
    assert _queue(temp_db, session_id) == REMAINDER


def test_suppression_flags_roll_back_with_the_queue_they_share_a_receipt_with(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-0000000000a2")
    manager = SessionVariableManager(temp_db)
    _fail_queue_write_once(monkeypatch)
    staged = _staged(
        session_id, {CARRIED_CONTEXT_VARIABLE: REMAINDER}, {"injected_memory_ids": ["m-b"]}
    )

    with pytest.raises(RuntimeError, match="queue write failed"):
        commit_staged_effects(
            temp_db, session_id=session_id, staged=staged, variable_manager=manager
        )

    assert "injected_memory_ids" not in manager.get_variables(session_id)
    assert _queue(temp_db, session_id) is None
    assert [r["state"] for r in _receipts(temp_db, session_id)] == ["prepared"]

    commit_staged_effects(temp_db, session_id=session_id, staged=staged, variable_manager=manager)
    variables = manager.get_variables(session_id)
    assert variables["injected_memory_ids"] == ["m-b"]
    assert variables[CARRIED_CONTEXT_VARIABLE] == REMAINDER


def test_a_losing_acknowledgment_mutates_no_variable(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-0000000000a3")
    manager = SessionVariableManager(temp_db)
    staged = _staged(session_id, {CARRIED_CONTEXT_VARIABLE: REMAINDER})
    commit_staged_effects(temp_db, session_id=session_id, staged=staged, variable_manager=manager)
    receipt = _receipts(temp_db, session_id)[0]
    manager.merge_variables(session_id, {CARRIED_CONTEXT_VARIABLE: BACKLOG})

    loser = acknowledge_and_apply_receipt(
        temp_db,
        receipt_id=str(receipt["receipt_id"]),
        delivery_generation=int(receipt["delivery_generation"]),
        message_manager=None,
        variable_manager=manager,
    )

    assert loser is None
    assert _queue(temp_db, session_id) == BACKLOG


class _Host(ChatMixin):
    """Minimal ChatMixin host, as the lifecycle parity suite builds one."""

    def __init__(self) -> None:
        self.clients: dict[Any, Any] = {}
        self._chat_sessions: dict[str, Any] = {}
        self._active_chat_tasks: dict[str, Any] = {}
        self.workflow_handler: Any = None
        self.event_handlers: Any = None
        self.webhook_dispatcher: Any = None
        self.hook_broadcaster: Any = None
        self.inter_session_msg_manager: Any = None
        self.mcp_manager: Any = None
        self.internal_manager: Any = None

    async def _send_error(
        self,
        websocket: object,
        message: str,
        request_id: str | None = None,
        code: str = "ERROR",
    ) -> None:
        pass


@pytest.mark.asyncio
async def test_a_lifecycle_payload_and_a_failing_paging_commit_for_one_output_roll_back_together(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifecycle B and paging A of one SDK prompt output share one boundary.

    The real _fire_lifecycle arms the lifecycle callback. The real _staged_hook with
    _settle_prompt_delivery then commits B's payload and A's queue through that callback.
    A's queue write raises, so B's suppression flag must not survive the withheld output.
    """
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-0000000000a4")
    manager = SessionVariableManager(temp_db)
    manager.merge_variables(session_id, {CARRIED_CONTEXT_VARIABLE: BACKLOG})
    chat = _ChatHooks(temp_db, session_id)
    host = _Host()
    host._chat_sessions["conv-1"] = chat
    handler = MagicMock()
    handler.evaluate.return_value = HookResponse(
        decision="allow",
        metadata={
            STAGED_EFFECTS_FIELD: {
                "session_id": session_id,
                "append_set_variables": {"injected_memory_ids": ["m-lifecycle"]},
            }
        },
    )
    handler._session_var_manager = manager
    host.workflow_handler = handler
    host.inter_session_msg_manager = MagicMock(get_undelivered_messages=MagicMock(return_value=[]))
    await host._fire_lifecycle("conv-1", HookEventType.BEFORE_AGENT, {"prompt": "hi"})
    assert chat._on_prompt_delivered is not None

    async def prompt_hook(inp: Any, tool_use_id: Any, ctx: Any) -> SyncHookJSONOutput:
        record_worker_staging(
            {
                "session_id": session_id,
                "session_variables": {CARRIED_CONTEXT_VARIABLE: REMAINDER},
            }
        )
        return {}

    staged_hook = chat._staged_hook(prompt_hook, settle=chat._settle_prompt_delivery)
    _fail_queue_write_once(monkeypatch)

    with pytest.raises(RuntimeError, match="queue write failed"):
        await staged_hook(cast(Any, {"prompt": "hi"}), None, cast(Any, None))

    variables = manager.get_variables(session_id)
    assert "injected_memory_ids" not in variables
    assert variables.get(CARRIED_CONTEXT_VARIABLE) == BACKLOG
    assert all(r["state"] != "acknowledged" for r in _receipts(temp_db, session_id))
    assert chat._on_prompt_delivered is not None


@pytest.mark.asyncio
async def test_a_paging_commit_that_raises_before_its_output_returns_ships_the_owed_context_once(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case (i): no lifecycle callback, so the commit runs before the output returns.

    The raise propagates out of the hook, the owed one-shot context stays owed, and the
    next prompt ships it once with the backlog drained.
    """
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-0000000000a5")
    chat = _ChatHooks(temp_db, session_id)
    await chat.prompt("allow", [("rule:policy", "o" * 6_000), ("rule:common", "c" * 5_000)])
    assert _queue(temp_db, session_id) == BACKLOG
    _owe_one_shot_context(chat)
    _fail_queue_write_once(monkeypatch)

    with pytest.raises(RuntimeError, match="queue write failed"):
        await chat.prompt("allow", [("rule:reminder", "be brief")])

    context = _delivered_context(await chat.prompt("allow", [("rule:reminder", "be brief")]))
    assert context.count("revise step 2") == 1
    assert context.count("c" * 5_000) == 1
    assert "<history>earlier turns</history>" in context
    assert _queue(temp_db, session_id) is None
