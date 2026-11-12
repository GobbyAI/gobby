"""Report schedule, semantic differences and confirmed-delivery baselines."""

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from gobby.status_report.delivery import (
    ReportStore,
    deliver_artifact,
    delta_data,
    report_mode,
    semantic_snapshot,
)
from gobby.status_report.model import IdleHolder, StalledSeat, StatusData, WorktreeRow
from gobby.storage.hub.protocol import HubDatabase

NOW = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
POLICY = {"full_report_minutes": "00-29 UTC", "delta_report_minutes": "30-59 UTC"}


def _data(*rows: WorktreeRow) -> StatusData:
    return StatusData(
        now=NOW,
        window_start=NOW - timedelta(minutes=30),
        in_flight=(),
        closed=(),
        escalated=(),
        landings=(),
        attention=(),
        decisions=(),
        stalled=(),
        worktrees=rows,
        load_5m=0,
        load_threshold=60,
        gaps=(),
    )


def _row(seq: int, title: str) -> WorktreeRow:
    return WorktreeRow(
        branch=f"fixture-{seq}",
        task_seq=seq,
        task_state="in_progress",
        ahead=1,
        behind=0,
        dirty=0,
        live_seat=True,
        last_commit_at=NOW,
        git_error=None,
        task_title=title,
    )


@pytest.mark.parametrize(
    "minute,expected", [(0, "full"), (29, "full"), (30, "delta"), (59, "delta")]
)
def test_report_mode_uses_installed_minute_ranges(minute: int, expected: str) -> None:
    assert report_mode(POLICY, NOW.replace(minute=minute)) == expected


def test_report_mode_honors_a_changed_installed_schedule() -> None:
    swapped = {"full_report_minutes": "30-59 UTC", "delta_report_minutes": "00-29 UTC"}
    assert report_mode(swapped, NOW) == "delta"
    assert report_mode(swapped, NOW.replace(minute=30)) == "full"


def test_delta_keeps_changed_rows_and_names_removed_rows() -> None:
    original = _data(_row(1, "Unchanged"), _row(2, "Before"), _row(3, "Removed"))
    current = _data(_row(1, "Unchanged"), _row(2, "After"), _row(4, "Added"))

    delta = delta_data(current, semantic_snapshot(original), NOW)

    assert [row.task_seq for row in delta.worktrees] == [2, 4]
    assert any("3" in removal for removal in getattr(delta, "removed", ()))


def test_advancing_clock_does_not_turn_an_idle_holder_into_a_change() -> None:
    holder = IdleHolder(
        task_seq=1,
        task_title="Held",
        session_id="fixture-holder",
        agent="fixture-builder",
        status="paused",
        idle_seconds=1800,
    )
    original = replace(_data(), idle_holders=(holder,))
    current = replace(
        original,
        now=NOW + timedelta(minutes=30),
        idle_holders=(replace(holder, idle_seconds=3600),),
    )

    delta = delta_data(current, semantic_snapshot(original), NOW)

    assert delta.idle_holders == ()


def test_delta_distinguishes_stalled_seats_on_the_same_task() -> None:
    first = StalledSeat(session_ref="fixture-first", task_title="Held", task_seq=1)
    second = replace(first, session_ref="fixture-second")
    original = replace(_data(), stalled=(first, second))
    current = replace(original, stalled=(second,))

    delta = delta_data(current, semantic_snapshot(original), NOW)

    assert delta.stalled == ()
    assert any("fixture-first" in removal for removal in delta.removed)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delivery_uses_captured_bytes_when_the_source_changes_during_send(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    store = ReportStore(temp_db, str(sample_project["id"]), str(uuid4()), "fixture-destination")
    source = tmp_path / "report.md"
    _publish(store, source, "reviewed")
    observed: list[str] = []

    async def send(delivery_path: Path | None = None) -> dict[str, Any]:
        source.write_text("swapped during delivery")
        observed.append((delivery_path or source).read_text())
        source.write_text("reviewed")
        return {"success": True, "message": {"id": str(uuid4()), "status": "sent"}}

    result = await deliver_artifact(store, source, send)

    assert result["success"] is True
    assert observed == ["reviewed"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retargeting_source_alias_cannot_acknowledge_another_run(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    store = ReportStore(temp_db, str(sample_project["id"]), str(uuid4()), "fixture-destination")
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first_id = _publish(store, first, "same rendered bytes")
    _publish(store, second, "same rendered bytes")
    alias = tmp_path / "alias.md"
    alias.symlink_to(first)

    async def send(delivery_path: Path) -> dict[str, Any]:
        alias.unlink()
        alias.symlink_to(second)
        return {"success": True, "message": {"id": str(uuid4()), "status": "sent"}}

    result = await deliver_artifact(store, alias, send)
    assert result["success"] is True
    baseline = store.baseline()
    assert baseline is not None and baseline.run_id == first_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancelled_caller_keeps_snapshot_until_delivery_finishes(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    store = ReportStore(temp_db, str(sample_project["id"]), str(uuid4()), "fixture-destination")
    source = tmp_path / "report.md"
    _publish(store, source, "reviewed")
    started = asyncio.Event()
    finish = asyncio.Event()
    delivered = asyncio.Event()
    paths: list[Path] = []
    observed: list[str] = []

    async def send(delivery_path: Path | None = None) -> dict[str, Any]:
        paths.append(delivery_path or source)
        started.set()
        await finish.wait()
        observed.append(paths[0].read_text())
        delivered.set()
        return {"success": True, "message": {"id": str(uuid4()), "status": "sent"}}

    delivery = asyncio.create_task(deliver_artifact(store, source, send))
    await asyncio.wait_for(started.wait(), 2)
    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await delivery
    source.write_text("changed after cancellation")
    assert paths[0].exists()
    finish.set()
    await asyncio.wait_for(delivered.wait(), 2)
    assert observed == ["reviewed"]
    assert store.baseline() is None


def _publish(store: ReportStore, path: Path, text: str) -> int:
    run_id = store.reserve("full", NOW)
    path.write_text(text)
    store.publish(
        run_id, path, hashlib.sha256(path.read_bytes()).hexdigest(), {"worktrees": {text: text}}
    )
    return run_id


@pytest.mark.integration
def test_only_delivered_full_runs_become_baselines_in_generation_order(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    store = ReportStore(temp_db, str(sample_project["id"]), str(uuid4()), "fixture-destination")
    old = tmp_path / "old.md"
    new = tmp_path / "new.md"
    old_id = _publish(store, old, "older")
    new_id = _publish(store, new, "newer")
    assert store.baseline() is None
    assert store.acknowledge(new_id, str(uuid4()), hashlib.sha256(b"newer").hexdigest())
    assert store.acknowledge(old_id, str(uuid4()), hashlib.sha256(b"older").hexdigest())
    baseline = store.baseline()
    assert baseline is not None
    assert baseline.run_id == new_id > old_id
    assert baseline.snapshot == {"worktrees": {"newer": "newer"}}


@pytest.mark.integration
def test_changed_artifact_cannot_advance_a_destination_scoped_baseline(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    machine_id = str(uuid4())
    store = ReportStore(temp_db, str(sample_project["id"]), machine_id, "fixture-first")
    path = tmp_path / "report.md"
    run_id = _publish(store, path, "reviewed")
    path.write_text("changed")
    with pytest.raises(ValueError, match="scope or digest"):
        store.capture_artifact(path)
    assert not store.acknowledge(run_id, str(uuid4()), hashlib.sha256(b"changed").hexdigest())
    assert store.baseline() is None
    path.write_text("reviewed")
    assert store.acknowledge(run_id, str(uuid4()), hashlib.sha256(b"reviewed").hexdigest())
    other = ReportStore(temp_db, str(sample_project["id"]), machine_id, "fixture-second")
    assert other.baseline() is None


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_send_does_not_advance_the_baseline(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    cancel: bool,
) -> None:
    store = ReportStore(temp_db, str(sample_project["id"]), str(uuid4()), "fixture-destination")
    path = tmp_path / "report.md"
    _publish(store, path, "pending")

    async def send(delivery_path: Path) -> dict[str, Any]:
        if cancel:
            raise asyncio.CancelledError
        return {"success": False, "error": "Synthetic delivery failure"}

    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await deliver_artifact(store, path, send)
    else:
        result = await deliver_artifact(store, path, send)
        assert result["success"] is False
    assert store.baseline() is None


@pytest.mark.integration
def test_delivered_delta_never_replaces_the_full_baseline(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    store = ReportStore(temp_db, str(sample_project["id"]), str(uuid4()), "fixture-destination")
    full = tmp_path / "full.md"
    full_id = _publish(store, full, "full")
    assert store.acknowledge(full_id, str(uuid4()), hashlib.sha256(b"full").hexdigest())
    delta_id = store.reserve("delta", NOW + timedelta(minutes=30))
    delta = tmp_path / "delta.md"
    delta.write_text("delta")
    store.publish(delta_id, delta, hashlib.sha256(delta.read_bytes()).hexdigest(), {})
    assert store.acknowledge(delta_id, str(uuid4()), hashlib.sha256(b"delta").hexdigest())
    baseline = store.baseline()
    assert baseline is not None and baseline.run_id == full_id
