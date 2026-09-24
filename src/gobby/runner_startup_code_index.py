"""Code-index startup health checks and maintenance worker ownership."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.config.code_index import CodeIndexConfig
    from gobby.runner import GobbyRunner
    from gobby.runner_lifecycle_startup import StartupTracker

logger = logging.getLogger(__name__)


async def _repair_code_index_bm25(
    runner: GobbyRunner,
    tracker: StartupTracker | None,
) -> bool:
    """Verify and repair required BM25 indexes before code-index workers start."""
    config = runner.config_runtime.capture().snapshot.active
    if not config.code_index.enabled:
        return True

    from gobby.code_index.bm25_health import (
        repair_bm25_indexes,
        unavailable_bm25_status,
    )
    from gobby.runner_init.services import mark_service_degraded

    database_url = runner.startup_config.database_url
    if database_url:
        try:
            status = await asyncio.to_thread(
                repair_bm25_indexes,
                database_url,
                timeout_seconds=config.code_index.maintenance_index_timeout_seconds,
            )
        except Exception as exc:
            logger.exception("Unexpected code-index BM25 recovery failure")
            status = unavailable_bm25_status(str(exc))
    else:
        status = unavailable_bm25_status("PostgreSQL database_url is not configured")

    if status["healthy"]:
        repaired = [item["name"] for item in status["indexes"] if item["repaired"]]
        if repaired:
            logger.warning("Repaired damaged code-index BM25 indexes: %s", ", ".join(repaired))
        else:
            logger.info("Code-index BM25 indexes verified healthy")
        if tracker:
            tracker.complete("Code-index BM25 healthy")
        return True

    mark_service_degraded(runner, "code_index_bm25")
    failures = [
        f"{item['name']}: {item['error'] or item['state']}"
        for item in status["indexes"]
        if item["state"] != "healthy"
    ]
    detail = "; ".join(failures)
    logger.error(
        "Code-index BM25 recovery failed; maintenance and sync workers will not start: %s. "
        "Run `gobby postgres repair-code-index`, then restart Gobby.",
        detail,
    )
    if tracker:
        tracker.error("Code-index BM25", detail)
    return False


def _start_code_index_tasks(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    def capture_code_index() -> CodeIndexConfig:
        return runner.config_runtime.capture().snapshot.active.code_index

    config = runner.config_runtime.capture().snapshot.active
    runner._code_index_task = None
    if runner.code_indexer:
        from gobby.code_index.maintenance import code_index_maintenance_loop

        summarizer = None
        if config.code_index.symbol_summary.enabled:
            from gobby.code_index.summarizer import SymbolSummarizer

            try:
                if runner.text_generation_service is None:
                    logger.warning("Skipping SymbolSummarizer: text generation service unavailable")
                else:
                    summarizer = SymbolSummarizer(
                        runner.text_generation_service,
                        config.code_index,
                    )
            except Exception as e:
                logger.warning("Failed to create SymbolSummarizer: %s", e)

        community_labeler = None
        if config.code_index.community_label.enabled:
            from gobby.code_index.community_labeler import CommunityLabeler

            try:
                if runner.text_generation_service is None:
                    logger.warning("Skipping CommunityLabeler: text generation service unavailable")
                else:
                    community_labeler = CommunityLabeler(
                        runner.text_generation_service,
                        config.code_index,
                    )
            except Exception as e:
                logger.warning("Failed to create CommunityLabeler: %s", e)

        shutdown_event = asyncio.Event()
        runner._code_index_shutdown = shutdown_event
        runner._code_index_task = asyncio.create_task(
            code_index_maintenance_loop(
                context=runner.code_indexer,
                shutdown_flag=shutdown_event,
                interval=config.code_index.maintenance_interval_seconds,
                summarizer=summarizer,
                symbol_summary_batch_size=config.code_index.symbol_summary.batch_size,
                community_labeler=community_labeler,
                community_label_batch_size=config.code_index.community_label.batch_size,
                capture_config=capture_code_index,
            ),
            name="code-index-maintenance",
        )
        if tracker:
            tracker.schedule("Code index maintenance")

    runner._sync_worker_task = None
    if runner.code_indexer:
        from gobby.code_index.sync_worker import sync_worker_loop

        sync_shutdown = asyncio.Event()
        runner._sync_worker_shutdown = sync_shutdown
        runner._sync_worker_task = asyncio.create_task(
            sync_worker_loop(
                storage=runner.code_indexer.storage,
                context=runner.code_indexer,
                config=config.code_index,
                shutdown_flag=sync_shutdown,
                run_db=runner.code_indexer.run_db,
                startup_ready=lambda: runner.http_server.services.startup_ready,
                capture_config=capture_code_index,
            ),
            name="code-index-sync-worker",
        )
        if tracker:
            tracker.schedule("Code index sync")
