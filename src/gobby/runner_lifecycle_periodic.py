"""Periodic maintenance task startup for the daemon lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.config.bin_freshness import BinFreshnessConfig
from gobby.config.wiki import WikiConfig, WikiRootConfig
from gobby.gwiki_gateway import GwikiGateway
from gobby.runner_lifecycle_startup import StartupTracker
from gobby.wiki.update_coordinator import WikiUpdateCoordinator
from gobby.wiki.watcher import WikiWatcher, WikiWatchScope

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner


DEFAULT_CHAT_ATTACHMENT_RETENTION_HOURS = 24
DEFAULT_CHAT_ATTACHMENT_GC_INTERVAL_MINUTES = 60


def _default_loops() -> dict[str, Any]:
    from gobby.runner_maintenance import (
        bin_freshness_loop,
        cleanup_chat_attachments_loop,
        cleanup_comms_messages_loop,
        cleanup_expired_isolation_loop,
        cleanup_zombie_messages_loop,
        drain_hook_inbox_loop,
        expire_approval_timeouts_loop,
        memory_reconcile_loop,
        metric_snapshot_loop,
        metrics_archive_loop,
        metrics_cleanup_loop,
        span_cleanup_loop,
        tmux_window_name_repair_loop,
    )

    return {
        "metrics_cleanup_loop": metrics_cleanup_loop,
        "metrics_archive_loop": metrics_archive_loop,
        "span_cleanup_loop": span_cleanup_loop,
        "memory_reconcile_loop": memory_reconcile_loop,
        "cleanup_zombie_messages_loop": cleanup_zombie_messages_loop,
        "cleanup_comms_messages_loop": cleanup_comms_messages_loop,
        "cleanup_chat_attachments_loop": cleanup_chat_attachments_loop,
        "cleanup_expired_isolation_loop": cleanup_expired_isolation_loop,
        "metric_snapshot_loop": metric_snapshot_loop,
        "bin_freshness_loop": bin_freshness_loop,
        "drain_hook_inbox_loop": drain_hook_inbox_loop,
        "expire_approval_timeouts_loop": expire_approval_timeouts_loop,
        "tmux_window_name_repair_loop": tmux_window_name_repair_loop,
    }


def _wiki_gateway_for_local_scope(
    wiki_config: WikiConfig,
    roots_by_scope: dict[str, WikiRootConfig],
) -> Callable[[str], GwikiGateway]:
    def gateway(scope: str) -> GwikiGateway:
        root = roots_by_scope.get(scope)
        return GwikiGateway(
            binary=getattr(wiki_config, "binary", None),
            project=str(root.path) if root is not None and scope == "project" else None,
            topic=_wiki_topic_name(scope),
            timeout_seconds=float(getattr(wiki_config, "timeout_seconds", 30.0)),
        )

    return gateway


def _wiki_topic_name(scope: str) -> str | None:
    if scope == "project":
        return None
    if scope.startswith("topic:"):
        topic = scope.removeprefix("topic:").strip()
        return topic or None
    return scope


def start_periodic_tasks(
    runner: GobbyRunner,
    *,
    tracker: StartupTracker | None,
    **loops: Any,
) -> None:
    """Start all lightweight periodic background tasks."""
    loops = {**_default_loops(), **loops}
    runner._metrics_cleanup_task = asyncio.create_task(
        loops["metrics_cleanup_loop"](runner.metrics_manager, lambda: runner._shutdown_requested),
        name="metrics-cleanup",
    )
    runner._metrics_archive_task = asyncio.create_task(
        loops["metrics_archive_loop"](
            runner.metrics_event_store, lambda: runner._shutdown_requested
        ),
        name="metrics-archive",
    )

    retention_days = 7
    if runner.config.telemetry and hasattr(runner.config.telemetry, "trace_retention_days"):
        retention_days = runner.config.telemetry.trace_retention_days
    runner._span_cleanup_task = asyncio.create_task(
        loops["span_cleanup_loop"](
            runner.database, lambda: runner._shutdown_requested, retention_days=retention_days
        ),
        name="span-cleanup",
    )

    runner._memory_reconcile_task = None
    if runner.memory_manager:
        runner._memory_reconcile_task = asyncio.create_task(
            loops["memory_reconcile_loop"](
                runner.memory_manager, lambda: runner._shutdown_requested
            ),
            name="memory-reconcile",
        )

    runner._zombie_messages_task = asyncio.create_task(
        loops["cleanup_zombie_messages_loop"](runner.database, lambda: runner._shutdown_requested),
        name="zombie-message-cleanup",
    )
    runner._comms_messages_task = asyncio.create_task(
        loops["cleanup_comms_messages_loop"](runner.database, lambda: runner._shutdown_requested),
        name="comms-message-cleanup",
    )
    db_executor = getattr(runner, "db_executor", None)
    chat_config = getattr(runner.config, "chat", None)
    attachment_retention_hours = getattr(
        chat_config,
        "attachment_unbound_retention_hours",
        DEFAULT_CHAT_ATTACHMENT_RETENTION_HOURS,
    )
    attachment_gc_interval_minutes = getattr(
        chat_config,
        "attachment_gc_interval_minutes",
        DEFAULT_CHAT_ATTACHMENT_GC_INTERVAL_MINUTES,
    )
    runner._chat_attachments_cleanup_task = asyncio.create_task(
        loops["cleanup_chat_attachments_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            retention_hours=attachment_retention_hours,
            interval_minutes=attachment_gc_interval_minutes,
            run_db=getattr(db_executor, "run", None),
        ),
        name="chat-attachment-cleanup",
    )
    runner._expired_isolation_task = asyncio.create_task(
        loops["cleanup_expired_isolation_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
        name="expired-isolation-cleanup",
    )
    runner._metric_snapshot_task = asyncio.create_task(
        loops["metric_snapshot_loop"](runner.database, lambda: runner._shutdown_requested),
        name="metric-snapshot",
    )
    runner._hook_inbox_task = asyncio.create_task(
        loops["drain_hook_inbox_loop"](runner.http_server.app, lambda: runner._shutdown_requested),
        name="hook-inbox-drain",
    )
    runner._bin_freshness_task = None
    bin_freshness_config = getattr(runner.config, "bin_freshness", None)
    if isinstance(bin_freshness_config, BinFreshnessConfig) and bin_freshness_config.enabled:
        runner._bin_freshness_task = asyncio.create_task(
            loops["bin_freshness_loop"](
                runner.database,
                bin_freshness_config,
                lambda: runner._shutdown_requested,
                run_db=getattr(db_executor, "run", None),
            ),
            name="bin-freshness",
        )

    runner._approval_timeout_task = None
    if runner.pipeline_execution_manager:
        runner._approval_timeout_task = asyncio.create_task(
            loops["expire_approval_timeouts_loop"](
                runner.pipeline_execution_manager, lambda: runner._shutdown_requested
            ),
            name="approval-timeout-expiry",
        )

    runner._tmux_window_repair_task = asyncio.create_task(
        loops["tmux_window_name_repair_loop"](
            getattr(runner, "session_manager", None),
            lambda: runner._shutdown_requested,
        ),
        name="tmux-window-repair",
    )

    runner._wiki_watcher = None
    runner._wiki_watcher_task = None
    wiki_config = getattr(runner.config, "wiki", None)
    if isinstance(wiki_config, WikiConfig) and wiki_config.enabled and wiki_config.roots:
        roots_by_scope = {root.scope: root for root in wiki_config.roots if root.path.exists()}
        scopes = [
            WikiWatchScope(name=root.scope, root=root.path) for root in roots_by_scope.values()
        ]
        if scopes:
            runner._wiki_watcher = WikiWatcher(
                scopes=scopes,
                coordinator=WikiUpdateCoordinator(
                    GwikiGateway(),
                    local_gateway_factory=_wiki_gateway_for_local_scope(
                        wiki_config,
                        roots_by_scope,
                    ),
                ),
                debounce_interval=wiki_config.debounce_interval,
                poll_interval=wiki_config.poll_interval,
                ignore_globs=wiki_config.ignore_globs,
            )
            runner._wiki_watcher_task = asyncio.create_task(
                runner._wiki_watcher.run(),
                name="wiki-watcher",
            )

    task_count = sum(
        1
        for task in (
            runner._metrics_cleanup_task,
            runner._metrics_archive_task,
            runner._span_cleanup_task,
            getattr(runner, "_memory_reconcile_task", None),
            runner._zombie_messages_task,
            runner._comms_messages_task,
            runner._chat_attachments_cleanup_task,
            runner._expired_isolation_task,
            runner._metric_snapshot_task,
            runner._hook_inbox_task,
            runner._bin_freshness_task,
            runner._approval_timeout_task,
            runner._tmux_window_repair_task,
            runner._wiki_watcher_task,
        )
        if task is not None
    )
    if tracker:
        tracker.schedule(f"Periodic maintenance ({task_count} tasks)")
