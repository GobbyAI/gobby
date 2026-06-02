from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from gobby.wiki.watcher import WikiWatcher, WikiWatchScope


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
    await watcher.flush_pending()

    assert coordinator.calls == []
