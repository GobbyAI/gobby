from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from gobby.wiki.watcher import WikiWatcher, WikiWatchScope

pytestmark = pytest.mark.unit


class RecordingCoordinator:
    def __init__(self) -> None:
        self.calls: list[dict[str, list[str]]] = []

    async def handle_local_changes(
        self, changed_paths_by_scope: dict[str, list[Path]]
    ) -> dict[str, Any]:
        self.calls.append(
            {
                scope: [path.name for path in paths]
                for scope, paths in changed_paths_by_scope.items()
            }
        )
        return {"index_handoff": {"status": "indexed"}}


class FailingCoordinator:
    def __init__(self) -> None:
        self.calls = 0

    async def handle_local_changes(
        self, changed_paths_by_scope: dict[str, list[Path]]
    ) -> dict[str, Any]:
        self.calls += 1
        raise RuntimeError("index failed")


class SequencedHandoffCoordinator:
    """Returns queued index_handoff payloads, then indexed handoffs."""

    def __init__(self, handoffs: list[dict[str, Any]]) -> None:
        self._handoffs = list(handoffs)
        self.calls: list[dict[str, list[str]]] = []

    async def handle_local_changes(
        self, changed_paths_by_scope: dict[str, list[Path]]
    ) -> dict[str, Any]:
        self.calls.append(
            {
                scope: [path.name for path in paths]
                for scope, paths in changed_paths_by_scope.items()
            }
        )
        if self._handoffs:
            return {"index_handoff": self._handoffs.pop(0)}
        return {"index_handoff": {"status": "indexed"}}


class BlockingCoordinator(RecordingCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_local_changes(
        self, changed_paths_by_scope: dict[str, list[Path]]
    ) -> dict[str, Any]:
        self.calls.append(
            {
                scope: [path.name for path in paths]
                for scope, paths in changed_paths_by_scope.items()
            }
        )
        self.started.set()
        await self.release.wait()
        return {"index_handoff": {"status": "indexed"}}


async def _eventually(predicate: Callable[[], bool]) -> None:
    deadline = asyncio.get_running_loop().time() + 2.0
    while True:
        if predicate():
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_debounce_groups_scope_changes(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    topic_root = tmp_path / "topic"
    project_root.mkdir()
    topic_root.mkdir()
    coordinator = RecordingCoordinator()
    watcher = WikiWatcher(
        scopes=[
            WikiWatchScope(name="project", root=project_root),
            WikiWatchScope(name="topic:notes", root=topic_root),
        ],
        coordinator=coordinator,
        debounce_interval=0.05,
        poll_interval=0.01,
    )

    task = asyncio.create_task(watcher.run())
    try:
        await _eventually(lambda: watcher._snapshots_initialized)
        (project_root / "a.md").write_text("a", encoding="utf-8")
        (topic_root / "b.md").write_text("b", encoding="utf-8")

        await _eventually(lambda: len(coordinator.calls) == 1)
    finally:
        await watcher.stop()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert coordinator.calls == [{"project": ["a.md"], "topic:notes": ["b.md"]}]


@pytest.mark.asyncio
async def test_initial_snapshot_is_populated_when_watcher_runs(tmp_path: Path) -> None:
    existing = tmp_path / "existing.md"
    existing.write_text("before", encoding="utf-8")
    coordinator = RecordingCoordinator()
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=coordinator,
        debounce_interval=0.01,
        poll_interval=0.01,
    )

    task = asyncio.create_task(watcher.run())
    try:
        await _eventually(lambda: watcher.health()["running"] is True)
        await watcher._scan_once()
        assert coordinator.calls == []

        existing.write_text("after", encoding="utf-8")

        await watcher._scan_once()
        await watcher.flush_pending()
    finally:
        await watcher.stop()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert coordinator.calls == [{"project": ["existing.md"]}]


@pytest.mark.asyncio
async def test_local_edit_triggers_index(tmp_path: Path) -> None:
    coordinator = RecordingCoordinator()
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=coordinator,
        debounce_interval=0.01,
        poll_interval=0.01,
    )

    await watcher.record_change(tmp_path / "note.md")
    await watcher.flush_pending()

    assert coordinator.calls == [{"project": ["note.md"]}]
    assert watcher.health()["last_index_time"] is not None


@pytest.mark.asyncio
async def test_scan_ignores_gwiki_written_paths_by_default(tmp_path: Path) -> None:
    """Refresh-written raw captures and vault state must not re-trigger indexing."""
    coordinator = RecordingCoordinator()
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=coordinator,
        debounce_interval=0.01,
        poll_interval=0.01,
    )

    (tmp_path / "raw" / "assets").mkdir(parents=True)
    (tmp_path / "_gwiki" / "compile").mkdir(parents=True)
    (tmp_path / "inbox").mkdir()
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "raw" / "src-refresh.md").write_text("refreshed capture")
    (tmp_path / "raw" / "assets" / "src-refresh.bin").write_text("asset")
    (tmp_path / "_gwiki" / "compile" / "state.json").write_text("{}")
    (tmp_path / "inbox" / "drop.md").write_text("inbox drop")
    (tmp_path / "knowledge" / "concept.md").write_text("real page")

    await watcher._scan_once()
    await watcher.flush_pending()

    assert coordinator.calls == [{"project": ["concept.md"]}]


@pytest.mark.asyncio
async def test_explicit_empty_ignore_globs_disables_default_ignores(tmp_path: Path) -> None:
    coordinator = RecordingCoordinator()
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=coordinator,
        debounce_interval=0.01,
        ignore_globs=[],
    )

    await watcher.record_change(tmp_path / "raw" / "capture.md")
    await watcher.flush_pending()

    assert coordinator.calls == [{"project": ["capture.md"]}]


@pytest.mark.asyncio
async def test_successful_flush_restarts_debounce_for_concurrent_changes(
    tmp_path: Path,
) -> None:
    coordinator = BlockingCoordinator()
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=coordinator,
        debounce_interval=60.0,
    )
    await watcher.record_change(tmp_path / "first.md")
    first_pending_since = watcher._pending_since

    flush = asyncio.create_task(watcher.flush_pending())
    await coordinator.started.wait()
    await watcher.record_change(tmp_path / "second.md")
    coordinator.release.set()
    await flush

    assert coordinator.calls == [{"project": ["first.md"]}]
    assert watcher.health()["pending_changes"] == 1
    assert watcher._pending_since is not None
    assert first_pending_since is not None
    assert watcher._pending_since > first_pending_since
    assert watcher._debounce_elapsed() is False


@pytest.mark.asyncio
async def test_successful_flush_preserves_repeated_same_path_change(tmp_path: Path) -> None:
    coordinator = BlockingCoordinator()
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=coordinator,
        debounce_interval=60.0,
    )
    path = tmp_path / "page.md"
    await watcher.record_change(path)

    flush = asyncio.create_task(watcher.flush_pending())
    await coordinator.started.wait()
    await watcher.record_change(path)
    coordinator.release.set()
    await flush

    assert coordinator.calls == [{"project": ["page.md"]}]
    assert watcher._pending == {"project": {path.resolve()}}
    assert watcher._pending_since is not None
    assert watcher._debounce_elapsed() is False


@pytest.mark.asyncio
async def test_cancelled_flush_restores_dispatched_paths(tmp_path: Path) -> None:
    coordinator = BlockingCoordinator()
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=coordinator,
        debounce_interval=60.0,
    )
    path = tmp_path / "page.md"
    await watcher.record_change(path)

    flush = asyncio.create_task(watcher.flush_pending())
    await coordinator.started.wait()
    flush.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flush

    assert watcher._pending == {"project": {path.resolve()}}
    assert watcher._pending_since is not None


@pytest.mark.asyncio
async def test_flush_keeps_pending_changes_when_coordinator_fails(tmp_path: Path) -> None:
    coordinator = FailingCoordinator()
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=coordinator,
        debounce_interval=0.01,
        poll_interval=0.01,
    )

    await watcher.record_change(tmp_path / "note.md")

    with pytest.raises(RuntimeError, match="index failed"):
        await watcher.flush_pending()

    health = watcher.health()
    assert coordinator.calls == 1
    assert health["pending_changes"] == 1
    assert health["pending_debounce"] is True


@pytest.mark.asyncio
async def test_stop_flushes_pending_changes(tmp_path: Path) -> None:
    coordinator = RecordingCoordinator()
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=coordinator,
        debounce_interval=60.0,
        poll_interval=0.01,
    )

    await watcher.record_change(tmp_path / "note.md")
    await watcher.stop()

    assert coordinator.calls == [{"project": ["note.md"]}]
    assert watcher.health()["pending_changes"] == 0


def test_snapshot_skips_transient_file_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = tmp_path / "note.md"
    note.write_text("note", encoding="utf-8")
    original_stat = Path.stat

    def flaky_stat(path: Path, *args: Any, **kwargs: Any):
        if path == note:
            raise OSError("vanished")
        return original_stat(path, *args, **kwargs)

    watcher = WikiWatcher(
        scopes=[],
        coordinator=RecordingCoordinator(),
        debounce_interval=0.01,
        poll_interval=0.01,
    )
    monkeypatch.setattr(Path, "stat", flaky_stat)

    snapshot = watcher._snapshot(WikiWatchScope(name="project", root=tmp_path))

    assert snapshot == {}


@pytest.mark.asyncio
async def test_scan_once_continues_after_scope_snapshot_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_root = tmp_path / "failed"
    good_root = tmp_path / "good"
    failed_root.mkdir()
    good_root.mkdir()
    (good_root / "note.md").write_text("note", encoding="utf-8")
    coordinator = RecordingCoordinator()
    watcher = WikiWatcher(
        scopes=[
            WikiWatchScope(name="failed", root=failed_root),
            WikiWatchScope(name="good", root=good_root),
        ],
        coordinator=coordinator,
        debounce_interval=0.01,
        poll_interval=0.01,
    )
    original_snapshot = watcher._snapshot

    def flaky_snapshot(scope: WikiWatchScope) -> dict[Path, tuple[int, int]]:
        if scope.name == "failed":
            raise OSError("scope vanished")
        return original_snapshot(scope)

    monkeypatch.setattr(watcher, "_snapshot", flaky_snapshot)

    await watcher._scan_once()
    await watcher.flush_pending()

    assert "failed" not in watcher._snapshots
    assert "good" in watcher._snapshots
    assert coordinator.calls == [{"good": ["note.md"]}]


@pytest.mark.asyncio
async def test_scan_once_continues_after_record_change_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    coordinator = RecordingCoordinator()
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=coordinator,
        debounce_interval=0.01,
        poll_interval=0.01,
    )
    watcher._snapshots["project"] = {}
    monkeypatch.setattr(watcher, "_snapshot", lambda _scope: {first: (1, 1), second: (2, 2)})
    calls: list[Path] = []

    async def record_change(path: Path) -> None:
        calls.append(path)
        if len(calls) == 1:
            raise OSError("record failed")

    monkeypatch.setattr(watcher, "record_change", record_change)

    await watcher._scan_once()

    assert set(calls) == {first, second}
    assert watcher._snapshots["project"] == {first: (1, 1), second: (2, 2)}


@pytest.mark.asyncio
async def test_scan_once_propagates_unexpected_record_change_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = tmp_path / "note.md"
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=RecordingCoordinator(),
        debounce_interval=0.01,
        poll_interval=0.01,
    )
    watcher._snapshots["project"] = {}
    monkeypatch.setattr(watcher, "_snapshot", lambda _scope: {note: (1, 1)})

    async def record_change(_path: Path) -> None:
        raise AssertionError("programmer error")

    monkeypatch.setattr(watcher, "record_change", record_change)

    with pytest.raises(AssertionError, match="programmer error"):
        await watcher._scan_once()


@pytest.mark.asyncio
async def test_ignores_noncanonical_churn(tmp_path: Path) -> None:
    coordinator = RecordingCoordinator()
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=coordinator,
        debounce_interval=0.01,
        poll_interval=0.01,
    )

    await watcher.record_change(tmp_path / "outputs" / "build.json")
    await watcher.record_change(tmp_path / "meta" / "health" / "status.json")
    await watcher.record_change(tmp_path / "meta" / "librarian" / "proposal-001.md")
    await watcher.record_change(tmp_path / "meta" / "upkeep" / "run-report.md")
    await watcher.record_change(tmp_path / "_meta" / "catalog.md")
    await watcher.flush_pending()

    assert coordinator.calls == []

    await watcher.record_change(tmp_path / "knowledge" / "topics" / "hooks.md")
    await watcher.flush_pending()

    assert coordinator.calls == [{"project": ["hooks.md"]}]


def test_ignored_treats_out_of_scope_resolution_as_ignored(tmp_path: Path) -> None:
    scope_root = tmp_path / "wiki"
    scope_root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = scope_root / "escape.md"
    link.symlink_to(outside)
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=scope_root)],
        coordinator=RecordingCoordinator(),
        debounce_interval=0.01,
        poll_interval=0.01,
    )
    scope = watcher._scopes[0]

    assert watcher._ignored(scope, link) is True
    assert watcher._ignored(scope, scope_root / "note.md") is False


@pytest.mark.asyncio
async def test_scan_survives_out_of_scope_symlink(tmp_path: Path) -> None:
    scope_root = tmp_path / "wiki"
    scope_root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (scope_root / "escape.md").symlink_to(outside)
    (scope_root / "note.md").write_text("note", encoding="utf-8")
    coordinator = RecordingCoordinator()
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=scope_root)],
        coordinator=coordinator,
        debounce_interval=0.01,
        poll_interval=0.01,
    )
    watcher._snapshots["project"] = {}

    await watcher._scan_once()
    await watcher.flush_pending()

    assert coordinator.calls == [{"project": ["note.md"]}]


def test_snapshot_skips_malformed_path_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = tmp_path / "bad.md"
    good = tmp_path / "good.md"
    bad.write_text("bad", encoding="utf-8")
    good.write_text("good", encoding="utf-8")
    watcher = WikiWatcher(
        scopes=[],
        coordinator=RecordingCoordinator(),
        debounce_interval=0.01,
        poll_interval=0.01,
    )
    original_resolve = Path.resolve

    def flaky_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        if path.name == "bad.md":
            raise ValueError("embedded null byte")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", flaky_resolve)

    snapshot = watcher._snapshot(WikiWatchScope(name="project", root=tmp_path))

    assert set(snapshot) == {good.resolve()}


@pytest.mark.asyncio
async def test_scan_once_continues_after_scope_snapshot_value_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_root = tmp_path / "failed"
    project_root = tmp_path / "project"
    topic_root = tmp_path / "topic"
    for root in (failed_root, project_root, topic_root):
        root.mkdir()
    (project_root / "note.md").write_text("note", encoding="utf-8")
    (topic_root / "guide.md").write_text("guide", encoding="utf-8")
    coordinator = RecordingCoordinator()
    watcher = WikiWatcher(
        scopes=[
            WikiWatchScope(name="failed", root=failed_root),
            WikiWatchScope(name="project", root=project_root),
            WikiWatchScope(name="topic:notes", root=topic_root),
        ],
        coordinator=coordinator,
        debounce_interval=0.01,
        poll_interval=0.01,
    )
    original_snapshot = watcher._snapshot

    def flaky_snapshot(scope: WikiWatchScope) -> dict[Path, tuple[int, int]]:
        if scope.name == "failed":
            raise ValueError("embedded null byte")
        return original_snapshot(scope)

    monkeypatch.setattr(watcher, "_snapshot", flaky_snapshot)

    await watcher._scan_once()
    await watcher.flush_pending()

    assert "failed" not in watcher._snapshots
    assert coordinator.calls == [{"project": ["note.md"], "topic:notes": ["guide.md"]}]


@pytest.mark.asyncio
async def test_scan_once_runs_snapshot_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poll-tick filesystem walk must not block the event loop."""
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=RecordingCoordinator(),
        debounce_interval=0.01,
        poll_interval=0.01,
    )
    walk_started = threading.Event()
    release_walk = threading.Event()

    def blocking_snapshot(scope: WikiWatchScope) -> dict[Path, tuple[int, int]]:
        walk_started.set()
        # Held open until the loop proves it stayed responsive; on the event
        # loop this wait would freeze every coroutine below until timeout.
        release_walk.wait(timeout=5.0)
        return {}

    monkeypatch.setattr(watcher, "_snapshot", blocking_snapshot)

    scan = asyncio.create_task(watcher._scan_once())
    try:
        await asyncio.wait_for(asyncio.to_thread(walk_started.wait, 5.0), timeout=6.0)
        # The walk is mid-flight in a worker thread; the loop must still
        # process callbacks and the scan must not have finished.
        loop_responsive = asyncio.Event()
        asyncio.get_running_loop().call_soon(loop_responsive.set)
        await asyncio.wait_for(loop_responsive.wait(), timeout=1.0)
        assert not scan.done()
    finally:
        release_walk.set()
        await scan


@pytest.mark.asyncio
async def test_initialize_snapshots_survives_value_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=RecordingCoordinator(),
        debounce_interval=0.01,
        poll_interval=0.01,
    )

    def failing_scopes() -> dict[str, dict[Path, tuple[int, int]]]:
        raise ValueError("embedded null byte")

    monkeypatch.setattr(watcher, "_snapshot_all_scopes", failing_scopes)

    await watcher._initialize_snapshots()

    assert watcher._snapshots == {}
    assert watcher._snapshots_initialized is True


@pytest.mark.asyncio
async def test_degraded_flush_keeps_unindexed_scopes_and_skips_timestamp(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_root = tmp_path / "project"
    topic_root = tmp_path / "topic"
    project_root.mkdir()
    topic_root.mkdir()
    coordinator = SequencedHandoffCoordinator(
        [
            {
                "status": "degraded",
                "results_by_scope": {"project": {"ok": True}},
                "failed_scope": "topic:notes",
                "degradation": {"type": "index_handoff_failed", "message": "boom"},
            }
        ]
    )
    watcher = WikiWatcher(
        scopes=[
            WikiWatchScope(name="project", root=project_root),
            WikiWatchScope(name="topic:notes", root=topic_root),
        ],
        coordinator=coordinator,
        debounce_interval=0.01,
        poll_interval=0.01,
    )
    await watcher.record_change(project_root / "a.md")
    await watcher.record_change(topic_root / "b.md")

    with caplog.at_level("WARNING", logger="gobby.wiki.watcher"):
        result = await watcher.flush_pending()

    assert result is not None
    assert result["index_handoff"]["status"] == "degraded"
    health = watcher.health()
    assert health["last_index_time"] is None
    assert health["pending_changes"] == 1
    assert health["pending_debounce"] is True
    assert set(watcher._pending) == {"topic:notes"}
    assert watcher._pending_since is not None
    warnings = [
        record for record in caplog.records if "Wiki index handoff degraded" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "topic:notes" in warnings[0].getMessage()
    assert "boom" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_degraded_flush_without_scope_results_keeps_all_pending(tmp_path: Path) -> None:
    coordinator = SequencedHandoffCoordinator(
        [
            {
                "status": "degraded",
                "degradation": {"type": "index_handoff_failed", "message": "gwiki timed out"},
            }
        ]
    )
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=coordinator,
        debounce_interval=0.01,
        poll_interval=0.01,
    )
    await watcher.record_change(tmp_path / "note.md")

    result = await watcher.flush_pending()

    assert result is not None
    health = watcher.health()
    assert health["last_index_time"] is None
    assert health["pending_changes"] == 1
    assert coordinator.calls == [{"project": ["note.md"]}]


@pytest.mark.asyncio
async def test_degraded_flush_retries_pending_scopes_on_next_flush(tmp_path: Path) -> None:
    coordinator = SequencedHandoffCoordinator(
        [
            {
                "status": "degraded",
                "degradation": {"type": "index_handoff_failed", "message": "boom"},
            }
        ]
    )
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=coordinator,
        debounce_interval=0.01,
        poll_interval=0.01,
    )
    await watcher.record_change(tmp_path / "note.md")

    await watcher.flush_pending()
    result = await watcher.flush_pending()

    assert result is not None
    assert result["index_handoff"]["status"] == "indexed"
    assert coordinator.calls == [{"project": ["note.md"]}, {"project": ["note.md"]}]
    health = watcher.health()
    assert health["pending_changes"] == 0
    assert health["last_index_time"] is not None
