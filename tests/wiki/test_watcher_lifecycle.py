from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby import runner_lifecycle_periodic, runner_lifecycle_shutdown
from gobby.config.app import DaemonConfig
from gobby.config.wiki import WikiConfig, WikiRootConfig
from gobby.runner_lifecycle_periodic import start_periodic_tasks
from gobby.runner_lifecycle_shutdown import _cancel_periodic_tasks
from gobby.wiki.watcher import WikiWatcher, WikiWatchScope

pytestmark = pytest.mark.unit


async def _idle_loop(*args: Any, **kwargs: Any) -> None:
    await asyncio.Event().wait()


def _runner(config: DaemonConfig) -> Any:
    return SimpleNamespace(
        config=config,
        metrics_manager=object(),
        metrics_event_store=object(),
        database=object(),
        memory_manager=None,
        http_server=SimpleNamespace(app=object()),
        pipeline_execution_manager=None,
        session_manager=None,
        db_executor=None,
        _shutdown_requested=False,
    )


def _loops() -> dict[str, Any]:
    return {
        "metrics_cleanup_loop": _idle_loop,
        "metrics_archive_loop": _idle_loop,
        "span_cleanup_loop": _idle_loop,
        "memory_reconcile_loop": _idle_loop,
        "cleanup_zombie_messages_loop": _idle_loop,
        "cleanup_comms_messages_loop": _idle_loop,
        "cleanup_chat_attachments_loop": _idle_loop,
        "cleanup_expired_isolation_loop": _idle_loop,
        "metric_snapshot_loop": _idle_loop,
        "drain_hook_inbox_loop": _idle_loop,
        "bin_freshness_loop": _idle_loop,
        "expire_approval_timeouts_loop": _idle_loop,
        "tmux_window_name_repair_loop": _idle_loop,
    }


@pytest.mark.asyncio
async def test_startup_registers_watcher_for_configured_scopes(tmp_path: Path) -> None:
    config = DaemonConfig(
        wiki=WikiConfig(
            roots=[WikiRootConfig(scope="project", path=tmp_path)],
            debounce_interval=0.01,
            poll_interval=0.01,
        )
    )
    runner = _runner(config)

    start_periodic_tasks(runner, tracker=None, **_loops())
    try:
        assert isinstance(runner._wiki_watcher, WikiWatcher)
        assert isinstance(runner._wiki_watcher_task, asyncio.Task)
        assert not runner._wiki_watcher_task.done()
    finally:
        await _cancel_periodic_tasks(runner)


@pytest.mark.asyncio
async def test_startup_indexes_local_changes_with_scoped_gateways(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    topic_root = tmp_path / "topic"
    project_root.mkdir()
    topic_root.mkdir()
    constructed_scopes: list[tuple[str | None, str | None]] = []

    class FakeGwikiGateway:
        def __init__(
            self,
            *,
            binary: str | None = None,
            project_root: str | Path | None = None,
            topic: str | None = None,
            timeout_seconds: float = 30.0,
        ) -> None:
            self.project = str(project_root) if project_root is not None else None
            self.topic = topic

        async def index(self) -> dict[str, Any]:
            constructed_scopes.append((self.project, self.topic))
            return {
                "ok": True,
                "payload": {"scope": {"project": self.project, "topic": self.topic}},
            }

    monkeypatch.setattr(runner_lifecycle_periodic, "GwikiGateway", FakeGwikiGateway)
    config = DaemonConfig(
        wiki=WikiConfig(
            roots=[
                WikiRootConfig(scope="project", path=project_root),
                WikiRootConfig(scope="topic:research", path=topic_root),
            ],
            debounce_interval=0.01,
            poll_interval=0.01,
        )
    )
    runner = _runner(config)

    start_periodic_tasks(runner, tracker=None, **_loops())
    try:
        assert isinstance(runner._wiki_watcher, WikiWatcher)
        await runner._wiki_watcher.record_change(project_root / "a.md")
        await runner._wiki_watcher.record_change(topic_root / "b.md")

        result = await runner._wiki_watcher.flush_pending()
    finally:
        await _cancel_periodic_tasks(runner)

    assert constructed_scopes == [(str(project_root), None), (None, "research")]
    assert result is not None
    assert result["index_handoff"]["status"] == "indexed"
    assert set(result["index_handoff"]["results_by_scope"]) == {
        f"project:{project_root.resolve()}",
        "topic:research",
    }


def test_watch_scope_names_disambiguate_project_roots(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Duplicate "project" scope labels map to per-root watch scopes; only true duplicates drop."""
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    topic_root = tmp_path / "topic"
    first_root.mkdir()
    second_root.mkdir()
    topic_root.mkdir()
    config = WikiConfig(
        roots=[
            WikiRootConfig(scope="project", path=first_root),
            WikiRootConfig(scope="project", path=second_root),
            WikiRootConfig(scope="project", path=first_root),
            WikiRootConfig(scope="topic:research", path=topic_root),
            WikiRootConfig(scope="project", path=tmp_path / "missing"),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="gobby.runner_lifecycle_periodic"):
        roots = runner_lifecycle_periodic._roots_by_watch_scope(config)

    assert {name: root.path for name, root in roots.items()} == {
        f"project:{first_root.resolve()}": first_root,
        f"project:{second_root.resolve()}": second_root,
        "topic:research": topic_root,
    }
    duplicate_warnings = [
        message for message in caplog.messages if "duplicate wiki root" in message
    ]
    assert len(duplicate_warnings) == 1
    assert str(first_root) in duplicate_warnings[0]


def test_roots_by_watch_scope_expands_user_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    wiki_root = home / "wiki"
    wiki_root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    config = WikiConfig(roots=[WikiRootConfig(scope="project", path=Path("~/wiki"))])

    roots = runner_lifecycle_periodic._roots_by_watch_scope(config)

    assert {name: root.path for name, root in roots.items()} == {
        f"project:{wiki_root.resolve()}": wiki_root
    }


@pytest.mark.asyncio
async def test_startup_watches_all_duplicate_scope_project_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two projects both configured as scope "project" must each stay watched and indexed."""
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    indexed_projects: list[str | None] = []

    class FakeGwikiGateway:
        def __init__(
            self,
            *,
            binary: str | None = None,
            project_root: str | Path | None = None,
            topic: str | None = None,
            timeout_seconds: float = 30.0,
        ) -> None:
            self.project = str(project_root) if project_root is not None else None
            self.topic = topic

        async def index(self) -> dict[str, Any]:
            indexed_projects.append(self.project)
            return {"ok": True, "payload": {}}

    monkeypatch.setattr(runner_lifecycle_periodic, "GwikiGateway", FakeGwikiGateway)
    config = DaemonConfig(
        wiki=WikiConfig(
            roots=[
                WikiRootConfig(scope="project", path=first_root),
                WikiRootConfig(scope="project", path=second_root),
            ],
            debounce_interval=0.01,
            poll_interval=0.01,
        )
    )
    runner = _runner(config)

    start_periodic_tasks(runner, tracker=None, **_loops())
    try:
        assert isinstance(runner._wiki_watcher, WikiWatcher)
        assert runner._wiki_watcher.health()["scope_count"] == 2
        await runner._wiki_watcher.record_change(first_root / "a.md")
        await runner._wiki_watcher.record_change(second_root / "b.md")
        result = await runner._wiki_watcher.flush_pending()
    finally:
        await _cancel_periodic_tasks(runner)

    assert indexed_projects == [str(first_root), str(second_root)]
    assert result is not None
    assert result["index_handoff"]["status"] == "indexed"
    assert set(result["index_handoff"]["results_by_scope"]) == {
        f"project:{first_root.resolve()}",
        f"project:{second_root.resolve()}",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("wiki_config", [WikiConfig(enabled=False), WikiConfig()])
async def test_startup_skips_watcher_when_disabled_or_unconfigured(
    wiki_config: WikiConfig,
) -> None:
    runner = _runner(DaemonConfig(wiki=wiki_config))

    start_periodic_tasks(runner, tracker=None, **_loops())
    try:
        assert runner._wiki_watcher is None
        assert runner._wiki_watcher_task is None
    finally:
        await _cancel_periodic_tasks(runner)


@pytest.mark.asyncio
async def test_shutdown_stops_watcher(tmp_path: Path) -> None:
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=SimpleNamespace(handle_local_changes=_idle_loop),
        debounce_interval=0.01,
        poll_interval=0.01,
    )
    runner = SimpleNamespace(
        _wiki_watcher=watcher,
        _wiki_watcher_task=asyncio.create_task(watcher.run()),
    )

    await _cancel_periodic_tasks(runner)

    assert runner._wiki_watcher is None
    assert runner._wiki_watcher_task is None
    assert watcher.health()["running"] is False


@pytest.mark.asyncio
async def test_shutdown_continues_when_watcher_stop_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingWatcher:
        async def stop(self) -> None:
            await asyncio.Event().wait()

    runner = SimpleNamespace(
        _wiki_watcher=HangingWatcher(),
        _wiki_watcher_task=None,
    )
    monkeypatch.setattr(runner_lifecycle_shutdown, "WIKI_WATCHER_STOP_TIMEOUT_SECONDS", 0.01)

    await _cancel_periodic_tasks(runner)

    assert runner._wiki_watcher is None
    assert runner._wiki_watcher_task is None


def test_watcher_health_accessor(tmp_path: Path) -> None:
    watcher = WikiWatcher(
        scopes=[WikiWatchScope(name="project", root=tmp_path)],
        coordinator=SimpleNamespace(handle_local_changes=_idle_loop),
        debounce_interval=0.01,
        poll_interval=0.01,
    )

    health = watcher.health()

    assert health == {
        "running": False,
        "scope_count": 1,
        "last_index_time": None,
        "pending_debounce": False,
        "pending_changes": 0,
    }


@pytest.mark.asyncio
async def test_watcher_task_failure_is_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = DaemonConfig(
        wiki=WikiConfig(
            roots=[WikiRootConfig(scope="project", path=tmp_path)],
            debounce_interval=0.01,
            poll_interval=0.01,
        )
    )
    runner = _runner(config)

    async def failing_run(self: WikiWatcher) -> None:
        raise RuntimeError("watcher exploded")

    monkeypatch.setattr(WikiWatcher, "run", failing_run)

    with caplog.at_level(logging.ERROR, logger="gobby.runner_lifecycle_periodic"):
        start_periodic_tasks(runner, tracker=None, **_loops())
        try:
            with pytest.raises(RuntimeError, match="watcher exploded"):
                await runner._wiki_watcher_task
        finally:
            await _cancel_periodic_tasks(runner)

    failures = [
        record for record in caplog.records if record.getMessage() == "Wiki watcher task failed"
    ]
    assert len(failures) == 1
    assert failures[0].exc_info is not None
    assert "watcher exploded" in str(failures[0].exc_info[1])


@pytest.mark.asyncio
async def test_watcher_task_cancellation_is_not_logged_as_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = asyncio.create_task(asyncio.Event().wait())
    task.add_done_callback(runner_lifecycle_periodic._log_wiki_watcher_failure)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert all(record.getMessage() != "Wiki watcher task failed" for record in caplog.records)
