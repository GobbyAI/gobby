"""Late replay must not execute an earlier turn's effects against a newer turn."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI

from gobby.hooks import envelope_dedupe, inbox, replay_fence
from gobby.hooks.replay_fence import archive_superseded_hook
from gobby.hooks.runtime_compat import SUPPORTED_HOOK_RESPONSE_CAPABILITY
from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer
from gobby.storage.hook_receipts import prepare_receipt, release_receipt
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.datetime import utc_now

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier", [False, True])
@pytest.mark.parametrize("identity", ["header", "context", "external"])
async def test_old_stop_preserves_new_turn_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    barrier: bool,
    identity: str,
) -> None:
    registered = session_manager.register(
        external_id="new-turn", source="codex", machine_id=None, project_id=sample_project["id"]
    )
    started_at = utc_now()
    TurnLifecycleReducer(session_manager).begin_turn(
        registered.id, TurnEvidence(source="codex", event_time=started_at)
    )
    session = session_manager.get(registered.id)
    assert session is not None
    prepared = prepare_receipt(
        temp_db, session_id=session.id, envelope_id="new-prepared", force_continue_execution_num=7
    )
    released = prepare_receipt(
        temp_db, session_id=session.id, envelope_id="new-released", force_continue_execution_num=8
    )
    assert (
        release_receipt(
            temp_db,
            receipt_id=released.receipt_id,
            delivery_generation=released.delivery_generation,
        )
        is not None
    )
    receipt_query = "SELECT * FROM hook_receipt_effects WHERE session_id = %s ORDER BY receipt_id"
    budget_query = (
        "SELECT * FROM hook_force_continue_budgets WHERE session_id = %s ORDER BY execution_num"
    )
    receipts_before = temp_db.fetchall(receipt_query, (session.id,))
    budgets_before = temp_db.fetchall(budget_query, (session.id,))
    assert {row["state"] for row in receipts_before} == {"prepared", "released"}
    assert len(budgets_before) == 2
    envelope: dict[str, Any] = {
        "schema_version": 1,
        "enqueued_at": (started_at - timedelta(minutes=1)).isoformat(),
        "hook_type": "Stop",
        "source": "codex",
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "input_data": {},
        "headers": {},
    }
    if identity == "header":
        envelope["headers"] = {"x-GOBBY-session-ID": session.id}
    elif identity == "context":
        envelope["input_data"] = {"terminal_context": {"gobby_session_id": session.id}}
    else:
        envelope["input_data"] = {"session_id": session.external_id}
        # An empty registration cache proves the fallback survives a restart.
        session_manager = SessionManager(temp_db)
    pending = tmp_path / "inbox"
    pending.mkdir()
    path = pending / "old-stop.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    app = FastAPI()
    app.state.hook_manager = SimpleNamespace(session_manager=session_manager)
    post = AsyncMock()
    monkeypatch.setattr(inbox, "_post_envelope", post)
    monkeypatch.setattr(inbox, "read_local_api_token", lambda: "isolated-test-token")
    if barrier:
        result = await inbox.drain_hook_inbox_barrier(app, pending, timeout_seconds=5)
        assert result.timed_out is False
        assert result.replayed == 1
    else:
        assert await inbox.drain_hook_inbox_once(app, pending, include_fresh=True) == 1
    post.assert_not_awaited()
    assert not path.exists()
    archive = inbox.get_hook_quarantine_dir(pending)
    assert json.loads((archive / path.name).read_text()) == envelope
    assert (
        json.loads((archive / f"{path.name}.meta.json").read_text())["reason"] == "superseded_hook"
    )
    assert temp_db.fetchall(receipt_query, (session.id,)) == receipts_before
    assert temp_db.fetchall(budget_query, (session.id,)) == budgets_before
    assert {row["receipt_id"] for row in receipts_before} == {
        prepared.receipt_id,
        released.receipt_id,
    }


@pytest.mark.parametrize(
    ("status", "activity", "event_time", "expected"),
    [
        ("active", datetime(2026, 1, 1, tzinfo=UTC), "2026-01-01T00:00:00Z", None),
        ("active", datetime(2026, 1, 1), "2026-01-01T01:00:00+01:00", None),
        ("active", None, "2026-01-01T00:00:00Z", None),
        ("active", None, "invalid", True),
        ("expired", None, "2026-01-01T00:00:00Z", True),
    ],
)
def test_replay_clock_and_terminal_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    activity: datetime | None,
    event_time: str,
    expected: bool | None,
) -> None:
    manager = Mock()
    manager.get.return_value = SimpleNamespace(id="known", status=status)
    app = SimpleNamespace(
        state=SimpleNamespace(hook_manager=SimpleNamespace(session_manager=manager))
    )
    archive = Mock(return_value=True)
    monkeypatch.setattr(
        replay_fence,
        "TurnLifecycleReducer",
        lambda _: SimpleNamespace(
            get=lambda _: SimpleNamespace(
                started_at=activity.replace(tzinfo=UTC) if activity else None
            )
        ),
    )
    result = archive_superseded_hook(
        app,
        {"headers": {"X-Gobby-Session-Id": "known"}, "enqueued_at": event_time},
        tmp_path / "event.json",
        archive,
    )
    assert result is expected
    assert archive.call_count == int(expected is True)


def test_lookup_failure_retains_hook_without_execution(tmp_path: Path) -> None:
    manager = Mock()
    manager.get.side_effect = RuntimeError("isolated hub unavailable")
    app = SimpleNamespace(
        state=SimpleNamespace(hook_manager=SimpleNamespace(session_manager=manager))
    )
    archive = Mock()
    assert (
        archive_superseded_hook(
            app, {"headers": {"X-Gobby-Session-Id": "known"}}, tmp_path / "event.json", archive
        )
        is False
    )
    archive.assert_not_called()


def test_unknown_first_session_is_admitted(tmp_path: Path) -> None:
    manager = Mock()
    manager.find_by_external_id_any_project.return_value = None
    app = SimpleNamespace(
        state=SimpleNamespace(hook_manager=SimpleNamespace(session_manager=manager))
    )
    archive = Mock()
    assert (
        archive_superseded_hook(
            app,
            {"input_data": {"session_id": "first"}, "source": "codex"},
            tmp_path / "event.json",
            archive,
        )
        is None
    )
    manager.find_by_external_id_any_project.assert_called_once_with("first", "codex")
    archive.assert_not_called()


@pytest.mark.asyncio
async def test_failed_archive_retains_stale_hook_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = Mock()
    manager.get.return_value = SimpleNamespace(id="known", status="active")
    app = FastAPI()
    app.state.hook_manager = SimpleNamespace(session_manager=manager)
    path = tmp_path / "old-stop.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "hook_type": "Stop",
                "source": "codex",
                "enqueued_at": "2026-01-01T00:00:00Z",
                "headers": {"X-Gobby-Session-Id": "known"},
            }
        )
    )
    archive = Mock(return_value=False)
    post = AsyncMock()
    monkeypatch.setattr(inbox, "_quarantine_file", archive)
    monkeypatch.setattr(inbox, "_post_envelope", post)
    monkeypatch.setattr(inbox, "read_local_api_token", lambda: "isolated-test-token")
    monkeypatch.setattr(
        replay_fence,
        "TurnLifecycleReducer",
        lambda _: SimpleNamespace(
            get=lambda _: SimpleNamespace(started_at=datetime(2026, 1, 2, tzinfo=UTC))
        ),
    )
    assert await inbox.drain_hook_inbox_once(app, tmp_path, include_fresh=True) == 0
    assert path.exists()
    archive.assert_called_once()
    post.assert_not_awaited()


def test_failed_archive_metadata_preserves_pending_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = tmp_path / "inbox"
    pending.mkdir()
    path = pending / "old-stop.json"
    path.write_text("{}")
    original_write = Path.write_text

    def write_text(current: Path, data: str, *args: Any, **kwargs: Any) -> int:
        if current.name.endswith(".meta.json"):
            raise PermissionError("isolated metadata write failure")
        return original_write(current, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    assert inbox._quarantine_file(path, reason="superseded_hook", detail="test") is False
    assert path.read_text() == "{}"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["fresh", "active", "processed"])
async def test_live_or_processed_envelope_precedes_stale_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    now = utc_now()
    envelope_id = f"n-{int(now.timestamp() * 1000):013d}-owner"
    path = tmp_path / f"{envelope_id}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "claude",
                "hook_type": "Stop",
                "enqueued_at": (now - timedelta(minutes=5)).isoformat(),
                "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
            }
        )
    )
    processed_dir = envelope_dedupe.get_processed_envelope_dir(tmp_path)
    if owner == "active":
        assert envelope_dedupe.claim_envelope_processing(envelope_id, processed_dir=processed_dir)
    elif owner == "processed":
        envelope_dedupe.mark_envelope_processed(envelope_id, processed_dir=processed_dir)
    fence = Mock(side_effect=AssertionError("live ownership must precede the stale fence"))
    post = AsyncMock()
    monkeypatch.setattr(inbox, "archive_superseded_hook", fence)
    monkeypatch.setattr(inbox, "_post_envelope", post)
    monkeypatch.setattr(inbox, "read_local_api_token", lambda: "isolated-test-token")
    assert (
        await inbox.drain_hook_inbox_once(FastAPI(), tmp_path, include_fresh=owner != "fresh") == 0
    )
    fence.assert_not_called()
    post.assert_not_awaited()
    assert path.exists() is (owner != "processed")
