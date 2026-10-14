"""The node and local drain share ordered hub decisions, without file effects."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from gobby.hooks import grok_pending_context, replay_fence
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("capable", [False, True])
def test_failed_session_lookup_retains(monkeypatch: pytest.MonkeyPatch, capable: bool) -> None:
    from gobby.hooks.replay_decision import decide_replay

    app, _ = _hub(monkeypatch)
    app.state.hook_manager.session_manager.get.side_effect = RuntimeError("hub unavailable")
    decision = decide_replay(
        app,
        "envelope",
        {
            "headers": {"X-Gobby-Session-Id": "session"},
            "response_capability": "hook-response.v1" if capable else None,
        },
        age_seconds=0,
        processed=False,
        live=False,
        machine_id="node",
    )
    assert decision.disposition == "retain"


@pytest.mark.parametrize(
    "caller_machine",
    ["37e78430-9ce8-447b-8a68-0688cde4a884", "37e78430-9ce8-447b-8a68-0688cde4a885"],
)
def test_below_floor_receipts_are_machine_scoped(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caller_machine: str,
) -> None:
    from gobby.hooks.replay_decision import decide_replay
    from gobby.storage import workspace_machine_scope
    from gobby.storage.hook_receipts import prepare_receipt
    from gobby.storage.machines import LocalMachineManager
    from tests.fixtures.postgres import TEST_USER_ID

    machine_id = "37e78430-9ce8-447b-8a68-0688cde4a884"
    LocalMachineManager(session_manager.db).upsert_seen(machine_id, TEST_USER_ID)
    monkeypatch.setattr(workspace_machine_scope, "require_machine_id", lambda: machine_id)
    session_id = session_manager.register_session(
        external_id="receipt-session",
        machine_id=machine_id,
        source="grok",
        project_id=sample_project["id"],
    )
    receipt = prepare_receipt(session_manager.db, session_id=session_id, envelope_id="floor")
    app = SimpleNamespace(state=SimpleNamespace(database=session_manager.db))
    decision = decide_replay(
        app,
        "floor",
        {},
        age_seconds=0,
        processed=False,
        live=False,
        machine_id=caller_machine,
    )
    assert decision.disposition == "below_floor"
    stored = session_manager.db.fetchone(
        "SELECT state FROM hook_receipt_effects WHERE receipt_id = %s",
        (receipt.receipt_id,),
    )
    assert stored is not None
    assert stored["state"] == (
        "terminal-undelivered" if caller_machine == machine_id else "prepared"
    )


@pytest.mark.asyncio
async def test_local_drain_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import time
    from unittest.mock import AsyncMock

    from gobby.hooks.envelope_dedupe import mark_envelope_processed
    from gobby.hooks.inbox import drain_hook_inbox_once

    app, values = _hub(monkeypatch)
    envelope_id = f"n-{int(time.time() * 1000):013d}-fresh"
    values["grok_pending_delivery"] = {
        "envelope_id": envelope_id,
        "components": [{"id": "brief", "text": "pending"}],
    }
    path = tmp_path / f"{envelope_id}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "grok",
                "hook_type": "Notification",
                "input_data": {"session_id": "external"},
                "headers": {"X-Gobby-Session-Id": "session"},
                "response_capability": "hook-response.v1",
            }
        )
    )
    mark_envelope_processed(envelope_id, processed_dir=tmp_path / "processed")
    post = AsyncMock()
    monkeypatch.setattr("gobby.hooks.inbox._post_envelope", post)
    monkeypatch.setattr("gobby.hooks.inbox.read_local_api_token", lambda: "test-token")
    assert await drain_hook_inbox_once(app, tmp_path) == 0
    assert path.exists()
    assert values["grok_pending_delivery"]["envelope_id"] == envelope_id
    post.assert_not_awaited()


def _hub(
    monkeypatch: pytest.MonkeyPatch, *, machine_id: str = "node"
) -> tuple[Any, dict[str, Any]]:
    session = SimpleNamespace(id="session", machine_id=machine_id, status="active")
    sessions = Mock()
    sessions.get.return_value = session
    sessions.find_by_external_id_any_project.return_value = session
    sessions.find_by_external_id.return_value = session
    values: dict[str, Any] = {}
    variables = Mock()
    variables.get_variable_subset.side_effect = lambda *_: dict(values)

    def mutate(_session: str, operation: Any, **_kwargs: Any) -> Any:
        return operation(values)[0]

    variables._mutate_variables.side_effect = mutate
    monkeypatch.setattr(grok_pending_context, "SessionVariableManager", lambda _: variables)
    monkeypatch.setattr(
        replay_fence,
        "TurnLifecycleReducer",
        lambda _: SimpleNamespace(get=lambda _: SimpleNamespace(started_at=None)),
    )
    manager = SimpleNamespace(_session_manager=sessions, session_manager=sessions)
    return SimpleNamespace(state=SimpleNamespace(hook_manager=manager)), values


@pytest.mark.parametrize(
    ("pending", "age", "processed", "live", "capable", "known", "expected"),
    [
        (True, 1.0, True, True, True, True, "retain"),
        (True, 3601.0, True, True, True, True, "requeued"),
        (False, 1.0, True, True, True, True, "unlink"),
        (False, 1.0, False, True, True, True, "retain"),
        (False, 1.0, False, False, True, True, "archived"),
        (False, 1.0, False, False, True, False, "execute"),
        (True, 1.0, False, True, False, True, "below_floor"),
    ],
)
def test_decision_order(
    monkeypatch: pytest.MonkeyPatch,
    pending: bool,
    age: float,
    processed: bool,
    live: bool,
    capable: bool,
    known: bool,
    expected: str,
) -> None:
    from gobby.hooks.replay_decision import decide_replay

    app, values = _hub(monkeypatch)
    if not known:
        app.state.hook_manager.session_manager.get.return_value = None
        app.state.hook_manager.session_manager.find_by_external_id.return_value = None
        app.state.hook_manager.session_manager.find_by_external_id_any_project.return_value = None
    component = {"id": "briefing", "text": "pending", "message_ids": []}
    if pending:
        values["grok_pending_delivery"] = {"envelope_id": "envelope", "components": [component]}
    envelope = {
        "source": "grok",
        "input_data": {"session_id": "external"},
        "headers": {"X-Gobby-Session-Id": "session"},
        "response_capability": "hook-response.v1" if capable else "hook-response.v0",
    }
    if expected == "below_floor":
        envelope["enqueued_at"] = "2026-01-01T00:00:00Z"
    decision = decide_replay(
        app,
        "envelope",
        envelope,
        age_seconds=age,
        processed=processed,
        live=live,
        machine_id="node",
    )
    assert decision.disposition == expected
    if expected == "requeued":
        assert "grok_pending_delivery" not in values
        assert values["grok_pending_briefing"] == [component]
    elif pending:
        assert values["grok_pending_delivery"]["envelope_id"] == "envelope"
    if live and expected == "retain" and not pending:
        assert decision.detail == "live"


@pytest.mark.parametrize("identity", ["header", "external"])
def test_foreign_rows_are_unknown(monkeypatch: pytest.MonkeyPatch, identity: str) -> None:
    from gobby.hooks.replay_decision import decide_replay

    app, values = _hub(monkeypatch, machine_id="foreign")
    values["grok_pending_delivery"] = {
        "envelope_id": "envelope",
        "components": [{"id": "briefing", "text": "pending"}],
    }
    app.state.hook_manager.session_manager.get.return_value.status = "expired"
    envelope: dict[str, Any] = {
        "source": "grok",
        "input_data": {"session_id": "external"},
        "enqueued_at": "2026-01-01T00:00:00Z",
        "response_capability": "hook-response.v1",
    }
    if identity == "header":
        envelope["headers"] = {"X-Gobby-Session-Id": "session"}
    decision = decide_replay(
        app,
        "envelope",
        envelope,
        age_seconds=3601.0,
        processed=False,
        live=False,
        machine_id="node",
    )
    assert decision.disposition == "execute"
    assert "grok_pending_delivery" in values
