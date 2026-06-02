from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.wiki import WikiConfig, WikiRootConfig
from gobby.runner_lifecycle_periodic import start_periodic_tasks
from gobby.runner_lifecycle_shutdown import _cancel_periodic_tasks
from gobby.wiki.watcher import WikiWatcher, WikiWatchScope


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
