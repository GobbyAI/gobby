from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from gobby.hooks.envelope_dedupe import (
    ENVELOPE_ID_HEADER,
    ENVELOPE_REPLAY_GRACE_SECONDS,
    claim_envelope_processing,
    finalize_envelope_processed,
    is_envelope_processed,
    is_inbox_envelope_fresh,
    mark_envelope_processed,
    read_envelope_marker,
    release_envelope_processing_claim,
)
from gobby.hooks.inbox import (
    HookInboxBarrierResult,
    _compute_sleep_seconds,
    _load_envelope,
    _post_envelope,
    _quarantine_file,
    drain_hook_inbox_barrier,
    drain_hook_inbox_once,
)
from gobby.storage import workspace_machine_scope
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _operator_token() -> Iterator[None]:
    with patch("gobby.hooks.inbox.read_local_api_token", return_value="test-operator-token"):
        yield


def _valid_envelope() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "hook_type": "session-start",
        "input_data": {},
        "source": "claude",
        "headers": {},
    }


@pytest.mark.parametrize("has_processed_marker", [False, True])
@pytest.mark.asyncio
async def test_grok_ack_pending_envelope_retains_then_settles_after_timeout(
    has_processed_marker: bool,
    tmp_path: Path,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = "37e78430-9ce8-447b-8a68-0688cde4a884"
    LocalMachineManager(session_manager.db).upsert_seen(machine_id, TEST_USER_ID)
    monkeypatch.setattr(workspace_machine_scope, "require_machine_id", lambda: machine_id)
    session_id = session_manager.register_session(
        external_id="grok-inbox",
        machine_id=machine_id,
        source="grok",
        project_id=sample_project["id"],
    )
    component = {"id": "turn:retained", "text": "retained", "message_ids": []}
    variables = SessionVariableManager(session_manager.db)
    variables.merge_variables(
        session_id,
        {
            "grok_pending_delivery": {
                "envelope_id": "n-0000000000001-grok-ack",
                "components": [component],
            }
        },
    )
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_id = "n-0000000000001-grok-ack"
    envelope_path = inbox_dir / f"{envelope_id}.json"
    envelope = _valid_envelope()
    envelope["source"] = "grok"
    envelope["input_data"] = {"session_id": "grok-inbox"}
    envelope["headers"] = {"X-Gobby-Session-Id": session_id}
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    processed_dir = inbox_dir / "processed"
    if has_processed_marker:
        mark_envelope_processed(envelope_id, processed_dir=processed_dir)
    app = FastAPI()
    app.state.hook_manager = MagicMock(_session_manager=session_manager)

    with patch("gobby.hooks.inbox._post_envelope", new_callable=AsyncMock) as post:
        post.return_value.status_code = 500
        replayed = await drain_hook_inbox_once(app, inbox_dir=inbox_dir)

    assert replayed == 0
    assert envelope_path.exists()
    post.assert_not_awaited()

    expired = datetime.now(UTC).timestamp() - 3_601
    os.utime(envelope_path, (expired, expired))
    with patch("gobby.hooks.inbox._post_envelope", new_callable=AsyncMock) as post:
        post.return_value.status_code = 500
        replayed = await drain_hook_inbox_once(app, inbox_dir=inbox_dir)

    assert replayed == 0
    assert not envelope_path.exists()
    assert read_envelope_marker(envelope_id, processed_dir=processed_dir) is None
    stored = variables.get_variables(session_id)
    assert "grok_pending_delivery" not in stored
    assert stored["grok_pending_briefing"] == [component]
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_barrier_timeout_reports_unresolved_run_and_session(tmp_path: Path) -> None:
    envelope = _valid_envelope()
    envelope["input_data"] = {
        "terminal_context": {
            "gobby_agent_run_id": "run-1",
            "gobby_session_id": "session-from-context",
        }
    }
    envelope["headers"] = {"X-Gobby-Session-Id": "session-from-header"}
    (tmp_path / "pending.json").write_text(json.dumps(envelope), encoding="utf-8")

    with patch(
        "gobby.hooks.inbox._drain_hook_inbox_once_locked",
        new=AsyncMock(return_value=0),
    ) as drain:
        result = await drain_hook_inbox_barrier(
            FastAPI(),
            tmp_path,
            timeout_seconds=0,
        )

    assert result.replayed == 0
    assert result.timed_out is True
    assert result.unresolved_run_ids == ("run-1",)
    assert result.unresolved_session_ids == (
        "session-from-context",
        "session-from-header",
    )
    drain.assert_awaited_once()
    assert drain.await_args is not None
    assert drain.await_args.kwargs == {"include_fresh": True}


@pytest.mark.asyncio
async def test_barrier_replays_fresh_envelope_end_to_end(tmp_path: Path) -> None:
    """The startup barrier replays envelopes still inside the freshness grace."""
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    timestamp_ms = int(datetime.now(UTC).timestamp() * 1000)
    envelope_id = f"n-{timestamp_ms}-abcd"
    envelope_path = inbox_dir / f"{envelope_id}.json"
    envelope_path.write_text(json.dumps(_valid_envelope()), encoding="utf-8")
    assert is_inbox_envelope_fresh(envelope_path)

    response = MagicMock(status_code=200)
    post = AsyncMock(return_value=response)
    with patch("gobby.hooks.inbox._post_envelope", new=post):
        result = await drain_hook_inbox_barrier(FastAPI(), inbox_dir, timeout_seconds=5.0)

    assert result.replayed == 1
    assert result.timed_out is False
    assert not envelope_path.exists()
    assert is_envelope_processed(envelope_id, processed_dir=inbox_dir / "processed")
    post.assert_awaited_once()
    assert post.await_args is not None
    assert post.await_args.kwargs == {"envelope_id": envelope_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("first_consumer", ["periodic", "barrier"])
async def test_periodic_and_barrier_drains_serialize_envelope_posts(
    tmp_path: Path,
    first_consumer: str,
) -> None:
    app = FastAPI()
    envelope_path = tmp_path / "pending.json"
    envelope_path.write_text(json.dumps(_valid_envelope()), encoding="utf-8")
    post_started = asyncio.Event()
    second_consumer_started = asyncio.Event()
    release_post = asyncio.Event()
    active_posts = 0
    posts_overlapped = False

    async def blocking_post(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal active_posts, posts_overlapped
        active_posts += 1
        posts_overlapped = posts_overlapped or active_posts > 1
        post_started.set()
        try:
            await asyncio.wait_for(release_post.wait(), timeout=1.0)
        finally:
            active_posts -= 1
        return MagicMock(status_code=200)

    post = AsyncMock(side_effect=blocking_post)

    async def run_waiting_periodic_drain() -> int:
        second_consumer_started.set()
        return await drain_hook_inbox_once(app, tmp_path)

    async def run_waiting_barrier_drain() -> HookInboxBarrierResult:
        second_consumer_started.set()
        return await drain_hook_inbox_barrier(app, tmp_path, timeout_seconds=1.0)

    periodic_task: asyncio.Task[int] | None = None
    barrier_task: asyncio.Task[HookInboxBarrierResult] | None = None
    with patch("gobby.hooks.inbox._post_envelope", new=post):
        async with asyncio.TaskGroup() as task_group:
            if first_consumer == "periodic":
                periodic_task = task_group.create_task(drain_hook_inbox_once(app, tmp_path))
            else:
                barrier_task = task_group.create_task(
                    drain_hook_inbox_barrier(app, tmp_path, timeout_seconds=1.0)
                )

            await asyncio.wait_for(post_started.wait(), timeout=1.0)
            if first_consumer == "periodic":
                barrier_task = task_group.create_task(run_waiting_barrier_drain())
            else:
                periodic_task = task_group.create_task(run_waiting_periodic_drain())

            await asyncio.wait_for(second_consumer_started.wait(), timeout=1.0)
            try:
                assert post.await_count == 1
                assert posts_overlapped is False
            finally:
                release_post.set()

    assert periodic_task is not None
    assert barrier_task is not None
    assert posts_overlapped is False
    assert post.await_count == 1
    assert envelope_path.exists() is False
    if first_consumer == "periodic":
        assert periodic_task.result() == 1
        assert barrier_task.result() == HookInboxBarrierResult(0, False, (), ())
    else:
        assert barrier_task.result() == HookInboxBarrierResult(1, False, (), ())
        assert periodic_task.result() == 0


@pytest.mark.asyncio
async def test_replayed_envelope_without_id_is_quarantined_and_barrier_settles(
    tmp_path: Path,
) -> None:
    """A 2xx replay with no envelope ID is quarantined instead of retained forever."""
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"
    envelope_path.write_text(json.dumps(_valid_envelope()), encoding="utf-8")

    response = MagicMock(status_code=200)
    with (
        patch("gobby.hooks.inbox._post_envelope", new=AsyncMock(return_value=response)),
        patch("gobby.hooks.inbox.envelope_id_from_inbox_path", return_value=None),
    ):
        replayed = await drain_hook_inbox_once(
            FastAPI(),
            inbox_dir=inbox_dir,
            include_fresh=True,
        )
        barrier = await drain_hook_inbox_barrier(FastAPI(), inbox_dir, timeout_seconds=5.0)

    assert replayed == 1
    assert not envelope_path.exists()
    assert (inbox_dir / "quarantine" / envelope_path.name).exists()
    meta = json.loads(
        (inbox_dir / "quarantine" / f"{envelope_path.name}.meta.json").read_text(encoding="utf-8")
    )
    assert meta["reason"] == "missing_envelope_id"
    assert barrier.replayed == 0
    assert barrier.timed_out is False


@pytest.mark.asyncio
async def test_replay_attaches_operator_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inbox replay authenticates as the operator, never a run capability.

    The replay drains envelopes for every session, so an inherited
    GOBBY_AGENT_API_TOKEN must not scope it to one run.
    """
    monkeypatch.setenv("GOBBY_AGENT_API_TOKEN", "scoped-agent-token")
    envelope = _valid_envelope()
    envelope["headers"] = {
        "authorization": "Bearer persisted-stale-token",
        "X-Gobby-Project-Id": "project-123",
    }
    response = MagicMock(status_code=200)
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)

    with (
        patch(
            "gobby.hooks.inbox.read_local_api_token",
            return_value="fresh-operator-token",
        ),
        patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=client),
    ):
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        replay_response = await _post_envelope(FastAPI(), envelope)

    assert replay_response is response
    assert client.post.await_args.args == ("/api/hooks/execute",)
    assert client.post.await_args.kwargs["json"] is envelope
    headers = client.post.await_args.kwargs["headers"]
    assert headers == {
        "X-Gobby-Project-Id": "project-123",
        "Authorization": "Bearer fresh-operator-token",
    }


@pytest.mark.asyncio
async def test_missing_required_token_warns_once_per_drain(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    for envelope_id in ("n-0000000000001-abcd", "n-0000000000002-abcd"):
        (inbox_dir / f"{envelope_id}.json").write_text(json.dumps(_valid_envelope()))

    response = MagicMock(status_code=500)
    with (
        patch("gobby.hooks.inbox.read_local_api_token", return_value=None),
        patch("gobby.hooks.inbox._post_envelope", new=AsyncMock(return_value=response)),
        caplog.at_level(logging.WARNING, logger="gobby.hooks.inbox"),
    ):
        await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    token_warnings = [record for record in caplog.records if "local_cli_token" in record.message]
    assert len(token_warnings) == 1
    assert "gobby auth token --rotate" in token_warnings[0].message


def test_malformed_nonempty_marker_logs_and_counts_processed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    processed_dir = tmp_path / "processed"
    mark_envelope_processed("n-0000000000001-abcd", processed_dir=processed_dir)
    marker = next(processed_dir.glob("*.json"))
    marker.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="gobby.hooks.envelope_dedupe"):
        record = read_envelope_marker(
            "n-0000000000001-abcd",
            processed_dir=processed_dir,
        )

    assert record == {"envelope_id": "n-0000000000001-abcd", "status": "processed"}
    assert is_envelope_processed("n-0000000000001-abcd", processed_dir=processed_dir)
    assert "Malformed processed hook envelope marker" in caplog.text


def test_response_less_mark_preserves_existing_terminal_response(tmp_path: Path) -> None:
    processed_dir = tmp_path / "processed"
    response = {"continue": False, "decision": "block", "reason": "task open"}

    mark_envelope_processed(
        "n-0000000000001-abcd",
        response=response,
        processed_dir=processed_dir,
    )
    mark_envelope_processed("n-0000000000001-abcd", processed_dir=processed_dir)

    record = read_envelope_marker("n-0000000000001-abcd", processed_dir=processed_dir)
    assert record is not None
    assert record["response"] == response


@pytest.mark.asyncio
async def test_drain_hook_inbox_replays_full_envelope_with_promoted_headers(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope = {
        "schema_version": 1,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "hook_type": "session-start",
        "input_data": {"session_id": "sess-123"},
        "source": "claude",
        "headers": {
            "X-Gobby-Project-Id": "proj-123",
            "X-Gobby-Session-Id": "sess-123",
        },
    }
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"
    envelope_path.write_text(json.dumps(envelope))

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=mock_client),
    ):
        replayed = await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    assert replayed == 1
    assert not envelope_path.exists()
    assert is_envelope_processed(
        "n-0000000000001-abcd",
        processed_dir=inbox_dir / "processed",
    )
    mock_client.post.assert_awaited_once_with(
        "/api/hooks/execute",
        json=envelope,
        headers={
            **envelope["headers"],
            ENVELOPE_ID_HEADER: "n-0000000000001-abcd",
            "Authorization": "Bearer test-operator-token",
        },
    )


@pytest.mark.asyncio
async def test_drain_hook_inbox_skips_already_processed_envelope(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope = {
        "schema_version": 1,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "hook_type": "session-start",
        "input_data": {"session_id": "sess-123"},
        "source": "claude",
        "headers": {},
    }
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"
    envelope_path.write_text(json.dumps(envelope))
    mark_envelope_processed(
        "n-0000000000001-abcd",
        processed_dir=inbox_dir / "processed",
    )

    mock_client = AsyncMock()
    mock_client.post = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        caplog.at_level(logging.DEBUG, logger="gobby.hooks.inbox"),
        patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=mock_client),
    ):
        replayed = await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    assert replayed == 0
    assert not envelope_path.exists()
    assert "Skipping already-processed hook inbox envelope" in caplog.text
    mock_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_hook_inbox_skips_fresh_envelope(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    timestamp_ms = int(datetime.now(UTC).timestamp() * 1000)
    envelope_path = inbox_dir / f"n-{timestamp_ms}-abcd.json"
    envelope_path.write_text(json.dumps(_valid_envelope()))

    mock_client = AsyncMock()
    mock_client.post = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=mock_client):
        replayed = await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    assert replayed == 0
    assert envelope_path.exists()
    mock_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_hook_inbox_skips_active_processing_marker(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_id = "n-0000000000001-abcd"
    envelope_path = inbox_dir / f"{envelope_id}.json"
    envelope_path.write_text(json.dumps(_valid_envelope()))
    claim_envelope_processing(envelope_id, processed_dir=inbox_dir / "processed")

    mock_client = AsyncMock()
    mock_client.post = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=mock_client):
        replayed = await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    assert replayed == 0
    assert envelope_path.exists()
    mock_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_hook_inbox_retains_live_owner_past_replay_grace(
    tmp_path: Path,
) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_id = "n-0000000000001-abcd"
    envelope_path = inbox_dir / f"{envelope_id}.json"
    envelope_path.write_text(json.dumps(_valid_envelope()))
    processed_dir = inbox_dir / "processed"
    claim_envelope_processing(envelope_id, processed_dir=processed_dir)
    marker_path = next(processed_dir.glob("*.json"))
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    aged = (datetime.now(UTC) - timedelta(seconds=ENVELOPE_REPLAY_GRACE_SECONDS + 1)).isoformat()
    marker["claimed_at"] = aged
    marker["renewed_at"] = aged
    marker["lease_expires_at"] = aged
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=mock_client):
        replayed = await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    assert replayed == 0
    assert envelope_path.exists()
    mock_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_hook_inbox_clears_dead_owner_expired_lease_and_replays(
    tmp_path: Path,
) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_id = "n-0000000000001-abcd"
    envelope_path = inbox_dir / f"{envelope_id}.json"
    envelope_path.write_text(json.dumps(_valid_envelope()))
    processed_dir = inbox_dir / "processed"
    claim_envelope_processing(envelope_id, processed_dir=processed_dir)
    marker_path = next(processed_dir.glob("*.json"))
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    aged = (datetime.now(UTC) - timedelta(seconds=ENVELOPE_REPLAY_GRACE_SECONDS + 1)).isoformat()
    marker["claimed_at"] = aged
    marker["renewed_at"] = aged
    marker["lease_expires_at"] = aged
    marker["owner_pid"] = 2_147_483_646
    marker["owner_create_time"] = 0.0
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=mock_client):
        replayed = await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    assert replayed == 1
    assert not envelope_path.exists()
    assert is_envelope_processed(envelope_id, processed_dir=processed_dir)
    mock_client.post.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 503])
async def test_drain_hook_inbox_keeps_failed_replay_files(
    tmp_path: Path,
    status_code: int,
) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope = {
        "schema_version": 1,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "hook_type": "session-start",
        "input_data": {},
        "source": "claude",
        "headers": {},
    }
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"
    envelope_path.write_text(json.dumps(envelope))

    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=mock_client):
        replayed = await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    assert replayed == 0
    assert envelope_path.exists()


@pytest.mark.asyncio
async def test_drain_hook_inbox_keeps_conflict_replay_files(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"
    envelope_path.write_text(json.dumps(_valid_envelope()))

    mock_response = MagicMock()
    mock_response.status_code = 409
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=mock_client):
        replayed = await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    assert replayed == 0
    assert envelope_path.exists()
    mock_client.post.assert_awaited_once()


def test_load_envelope_skips_quarantine_failure_without_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"
    envelope_path.write_text("{invalid", encoding="utf-8")

    with caplog.at_level("WARNING"):
        with patch("gobby.hooks.inbox.Path.write_text", side_effect=OSError("disk full")):
            envelope = _load_envelope(envelope_path)

    assert envelope is None
    assert not envelope_path.exists()
    assert (inbox_dir / "quarantine" / envelope_path.name).exists()
    assert "Skipping hook inbox file" in caplog.text


def test_load_envelope_quarantines_non_utf8_files(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"
    envelope_path.write_bytes(b"\xff\xfe\x00bad-json")

    envelope = _load_envelope(envelope_path)

    assert envelope is None
    assert not envelope_path.exists()
    quarantined = inbox_dir / "quarantine" / envelope_path.name
    assert quarantined.read_bytes() == b"\xff\xfe\x00bad-json"
    meta = json.loads((inbox_dir / "quarantine" / f"{envelope_path.name}.meta.json").read_text())
    assert meta["reason"] == "invalid_json"


def test_quarantine_missing_file_is_handled_as_race(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"

    with caplog.at_level(logging.WARNING, logger="gobby.hooks.inbox"):
        quarantined = _quarantine_file(envelope_path, reason="invalid_json", detail="missing")

    assert quarantined is True
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_compute_sleep_seconds_clamps_negative_jitter() -> None:
    with patch("gobby.hooks.inbox._JITTER_RANDOM.uniform", return_value=-10.0):
        assert _compute_sleep_seconds(interval_seconds=5, jitter_seconds=10.0) == 0.0


def test_release_envelope_processing_claim_allows_retry(tmp_path: Path) -> None:
    processed_dir = tmp_path / "processed"
    envelope_id = "n-0000000000001-retry"
    assert claim_envelope_processing(envelope_id, processed_dir=processed_dir) is True

    assert release_envelope_processing_claim(envelope_id, processed_dir=processed_dir) is True
    assert read_envelope_marker(envelope_id, processed_dir=processed_dir) is None
    assert claim_envelope_processing(envelope_id, processed_dir=processed_dir) is True


def test_release_envelope_processing_claim_preserves_finalized_or_absent_marker(
    tmp_path: Path,
) -> None:
    processed_dir = tmp_path / "processed"
    envelope_id = "n-0000000000001-finalized"
    assert claim_envelope_processing(envelope_id, processed_dir=processed_dir) is True
    token = read_envelope_marker(envelope_id, processed_dir=processed_dir)
    assert token is not None
    owner_token = token.get("owner_token")
    assert isinstance(owner_token, str) and owner_token
    assert (
        finalize_envelope_processed(envelope_id, owner_token, processed_dir=processed_dir) is True
    )

    assert release_envelope_processing_claim(envelope_id, processed_dir=processed_dir) is False
    marker = read_envelope_marker(envelope_id, processed_dir=processed_dir)
    assert marker is not None
    assert marker["status"] == "processed"
    assert release_envelope_processing_claim("", processed_dir=processed_dir) is False
