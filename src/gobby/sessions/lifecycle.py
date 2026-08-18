"""
Session lifecycle manager.

Handles background jobs for:
- Expiring stale sessions
- Processing transcripts for expired sessions
"""

import asyncio
import inspect
import logging
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar, cast

from gobby.config.app import DaemonConfig
from gobby.config.features import KnowledgeGraphQueueConfig
from gobby.config.persistence import MemoryDreamConfig
from gobby.config.runtime import RuntimeActiveBundle
from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions.transcript_processing import TranscriptProcessingMixin
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import SESSION_REVIVAL_HORIZON_HOURS
from gobby.storage.token_events import TokenEventStore

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SessionLifecycleManager(TranscriptProcessingMixin):
    """
    Manages session lifecycle background jobs.

    Two independent jobs:
    1. expire_stale_sessions - marks old active/paused sessions as expired
    2. process_pending_transcripts - processes transcripts for expired sessions
    """

    def __init__(
        self,
        db: HubDatabase,
        capture_bundle: Callable[[], RuntimeActiveBundle],
    ):
        self.db = db
        self._capture_bundle = capture_bundle
        self.session_manager = SessionManager(db)
        self.token_event_store = TokenEventStore(db)

        self._running = False
        self._expire_task: asyncio.Task[None] | None = None
        self._process_task: asyncio.Task[None] | None = None
        self._kg_queue_task: asyncio.Task[None] | None = None

    def _capture_active(self) -> DaemonConfig:
        return self._capture_bundle().snapshot.active

    @property
    def memory_manager(self) -> Any | None:
        """Resolve the current runtime epoch's memory manager per use."""
        service = self._capture_bundle().services.get("memory_services")
        return getattr(service, "memory_manager", None)

    @property
    def llm_service(self) -> Any | None:
        """Resolve the current runtime epoch's LLM service per use."""
        service = self._capture_bundle().services.get("ai_services")
        return getattr(service, "llm_service", None)

    async def _run_memory_db(
        self,
        func: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Run memory DB work on the memory manager's bounded executor when available."""
        run_db = getattr(self.memory_manager, "run_db", None)
        if callable(run_db):
            result = run_db(func, *args, **kwargs)
            if inspect.isawaitable(result):
                return await cast(Awaitable[T], result)
            return cast(T, result)
        return await asyncio.to_thread(func, *args, **kwargs)

    async def start(self) -> None:
        """Start background jobs."""
        if self._running:
            return

        self._running = True

        # Start expire job
        self._expire_task = asyncio.create_task(
            self._expire_loop(),
            name="session-lifecycle-expire",
        )

        # Start process job
        self._process_task = asyncio.create_task(
            self._process_loop(),
            name="session-lifecycle-process",
        )

        # Start KG queue processing job (if memory manager has KG service)
        if self.memory_manager and getattr(self.memory_manager, "kg_service", None):
            self._kg_queue_task = asyncio.create_task(
                self._kg_queue_loop(),
                name="session-lifecycle-kg-queue",
            )

        active = self._capture_active()
        logger.info(
            "SessionLifecycleManager started "
            "(expire every %sm, process every %sm, kg_queue every %sm)",
            active.session_lifecycle.expire_check_interval_minutes,
            active.session_lifecycle.transcript_processing_interval_minutes,
            active.knowledge_graph_queue.interval_minutes,
        )

    async def stop(self, drain_timeout: float = 1.0) -> None:
        """Stop background jobs."""
        self._running = False

        tasks = [t for t in [self._expire_task, self._process_task, self._kg_queue_task] if t]
        for task in tasks:
            task.cancel()

        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=drain_timeout)
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            for task in pending:
                task.add_done_callback(self._consume_stopped_task)
            if pending:
                logger.debug(
                    "SessionLifecycleManager stop continuing with %d cancelled task(s) draining",
                    len(pending),
                )

        self._expire_task = None
        self._process_task = None
        self._kg_queue_task = None

        logger.info("SessionLifecycleManager stopped")

    @staticmethod
    def _consume_stopped_task(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("SessionLifecycleManager background task ended during stop: %s", exc)

    async def _expire_loop(self) -> None:
        """Background loop for expiring stale sessions."""
        while self._running:
            active = self._capture_active()
            try:
                await self._expire_stale_sessions(active.session_lifecycle)
            except Exception as e:
                logger.error("Error in expire loop: %s", e)

            try:
                await self._purge_soft_deleted_definitions()
            except Exception as e:
                logger.error("Error purging soft-deleted definitions: %s", e)

            try:
                await self._purge_dream_hidden_memories(active.memory.dream)
            except Exception as e:
                logger.error("Error purging dream-hidden memories: %s", e)

            try:
                await self._sweep_digest_backlogs(active)
            except Exception as e:
                logger.error("Error sweeping digest backlogs: %s", e)

            try:
                await asyncio.sleep(active.session_lifecycle.expire_check_interval_minutes * 60)
            except asyncio.CancelledError:
                break

    async def _process_loop(self) -> None:
        """Background loop for processing pending transcripts."""
        while self._running:
            active = self._capture_active()
            try:
                await self._process_pending_transcripts(active)
            except Exception as e:
                logger.error("Error in process loop: %s", e)

            try:
                await asyncio.sleep(
                    active.session_lifecycle.transcript_processing_interval_minutes * 60
                )
            except asyncio.CancelledError:
                break

    async def _kg_queue_loop(self) -> None:
        """Background loop for processing pending KG graph memories."""
        while self._running:
            config = self._capture_active().knowledge_graph_queue
            try:
                await self._process_pending_graph_memories(config)
            except Exception as e:
                logger.error("Error in KG queue loop: %s", e)

            try:
                await asyncio.sleep(config.interval_minutes * 60)
            except asyncio.CancelledError:
                break

    async def _process_pending_graph_memories(self, config: KnowledgeGraphQueueConfig) -> int:
        """Process queued memories for KG extraction.

        Runs on a slow cadence (default 30 min). Processes memories
        sequentially to avoid bursty LLM calls.
        """
        if not self.memory_manager:
            return 0

        kg_service = getattr(self.memory_manager, "kg_service", None)
        if not kg_service:
            return 0

        pending = await self._run_memory_db(
            self.memory_manager.get_pending_graph_memories,
            limit=config.batch_size,
        )
        if not pending:
            return 0

        processed = 0
        max_deterministic_attempts = config.max_deterministic_attempts
        for memory in pending:
            try:
                result = await kg_service.add_to_graph(
                    memory.content,
                    memory_id=memory.id,
                    project_id=memory.project_id,
                    is_global=memory.is_global,
                )
            except Exception as e:
                logger.warning("KG processing failed for memory %s: %s", memory.id, e)
                try:
                    await self._run_memory_db(
                        self.memory_manager.record_graph_failure,
                        memory.id,
                        deterministic=False,
                        max_attempts=max_deterministic_attempts,
                    )
                except Exception as record_error:
                    logger.warning(
                        "Failed to persist KG retry state for memory %s: %s",
                        memory.id,
                        record_error,
                    )
                continue

            try:
                if result.status in ("success", "noop_no_entities"):
                    await self._run_memory_db(self.memory_manager.mark_graph_processed, memory.id)
                    processed += 1
                else:
                    deterministic = result.status == "deterministic_failure"
                    queue_status = await self._run_memory_db(
                        self.memory_manager.record_graph_failure,
                        memory.id,
                        deterministic=deterministic,
                        max_attempts=max_deterministic_attempts,
                    )
                    if queue_status == "failed":
                        logger.error(
                            "KG processing permanently failed for memory %s after %s attempts",
                            memory.id,
                            max_deterministic_attempts,
                        )
            except Exception as e:
                logger.warning("Failed to persist KG state for memory %s: %s", memory.id, e)

        if processed > 0:
            logger.debug("Processed %s memories for knowledge graph", processed)

        return processed

    async def _expire_stale_sessions(self, config: SessionLifecycleConfig) -> int:
        """Run the full session expiry pipeline, including zero-message cleanup."""
        # First, pause active sessions that have been idle too long
        # This catches orphaned sessions that never got AFTER_AGENT hook
        paused = self.session_manager.pause_inactive_active_sessions(
            timeout_minutes=config.active_session_pause_minutes
        )

        # Expire orphaned handoff_ready sessions (in-place compact restarts
        # complete within seconds, so 30 min is generous). Workflow state is
        # kept for revival; reclaim it only after the revival horizon.
        orphaned = self.session_manager.expire_orphaned_handoff_sessions(timeout_minutes=30)
        pruned_workflows = self.session_manager.prune_stale_compact_workflow_instances(
            retention_hours=SESSION_REVIVAL_HORIZON_HOURS
        )
        if pruned_workflows:
            logger.info("Pruned %s stale compact workflow instances", pruned_workflows)
        self.session_manager.cleanup_expired_session_state()

        # Then expire sessions that have been paused/active for too long
        expired = self.session_manager.expire_stale_sessions(
            timeout_hours=config.stale_session_timeout_hours
        )

        # Zero-message sessions created by spurious SESSION_START events can be
        # cleaned up much faster than the normal 24h stale-session sweep.
        fast_expired = self.session_manager.expire_empty_sessions(timeout_hours=2)
        pruned = self.session_manager.prune_empty_sessions(min_age_hours=1)

        # Clean up stale prompt files (run in thread to avoid blocking)
        await asyncio.to_thread(self._cleanup_prompt_files)

        return paused + orphaned + expired + fast_expired + pruned

    def _cleanup_prompt_files(self, max_age_seconds: int = 3600) -> int:
        """Delete prompt files older than max_age_seconds.

        Prompt files are read immediately by spawned agents, so any file
        older than 1 hour is safe to remove. Age-based cleanup also catches
        orphaned files from crashed sessions.
        """
        prompt_dir = Path(tempfile.gettempdir()) / "gobby-prompts"
        if not prompt_dir.is_dir():
            return 0

        now = time.time()
        removed = 0
        try:
            for path in prompt_dir.iterdir():
                try:
                    if now - path.stat().st_mtime > max_age_seconds:
                        path.unlink()
                        removed += 1
                except OSError:
                    pass
        except OSError:
            pass  # Handle directory access errors

        if removed > 0:
            logger.info("Cleaned up %s stale prompt file(s)", removed)
        return removed

    async def _purge_soft_deleted_definitions(self) -> None:
        """Permanently remove definitions that were soft-deleted more than 30 days ago."""
        from gobby.storage.definitions.agents import AgentDefinitionManager
        from gobby.storage.definitions.pipelines import PipelineDefinitionManager
        from gobby.storage.definitions.rules import RuleDefinitionManager
        from gobby.storage.definitions.variables import SessionVariableDefaultManager

        for manager_cls in (
            RuleDefinitionManager,
            AgentDefinitionManager,
            SessionVariableDefaultManager,
            PipelineDefinitionManager,
        ):
            try:
                await asyncio.to_thread(
                    manager_cls(self.db).purge_deleted,
                    older_than_days=30,
                )
            except Exception as e:
                logger.error(
                    "Failed to purge soft-deleted %s: %s",
                    getattr(manager_cls, "__name__", manager_cls),
                    e,
                )

    async def _purge_dream_hidden_memories(self, config: MemoryDreamConfig) -> None:
        """Hard-purge aged dream-hidden memories and prune dream run/snapshot history.

        Runs independently of ``dream.enabled`` so rows that dream soft-hid while it
        was on are still reclaimed after it is switched off. Each action class
        (``delete``/``review``) has its own grace window; the facade purge also
        reconciles secondary stores (Qdrant, knowledge graph) for the removed rows.
        Stale run/snapshot history is pruned by ``run_retention_days`` (snapshots
        cascade with their run).
        """
        manager = self.memory_manager

        purge_hidden = getattr(manager, "purge_dream_hidden", None)
        if callable(purge_hidden):
            for action, grace_days in (
                ("delete", config.purge_delete_after_days),
                ("review", config.purge_review_after_days),
            ):
                try:
                    result = purge_hidden(action, grace_days)
                    if inspect.isawaitable(result):
                        await result
                except Exception as e:
                    logger.error("Failed to purge dream-hidden %s memories: %s", action, e)

        try:
            from gobby.memory.dream.storage import MemoryDreamStore

            store = MemoryDreamStore(self.db)
            await self._run_memory_db(store.prune_runs, config.run_retention_days)
        except Exception as e:
            logger.error("Failed to prune dream run history: %s", e)

    async def _sweep_digest_backlogs(
        self,
        config: DaemonConfig,
        *,
        max_sessions: int = 10,
        max_batches_per_session: int = 3,
    ) -> None:
        """Drain digest backlogs for active sessions the turn-start catch-up missed.

        Turn-start catch-up only drains sessions that keep prompting; sessions
        that went quiet after a provider outage keep a permanent gap. Each sweep
        cycle runs a bounded number of catch-up batches per candidate session;
        the per-session digest lock and input-hash dedupe make this safe against
        concurrent turn-end digests.
        """
        digest_config = config.digest
        if not (config.memory.enabled and digest_config.enabled):
            return
        memory_manager = self.memory_manager
        llm_service = self.llm_service
        if memory_manager is None or llm_service is None:
            return

        threshold = digest_config.backlog_sweep_min_undigested
        rows = await asyncio.to_thread(
            self.db.fetchall,
            "SELECT id FROM sessions "
            "WHERE status = 'active' AND transcript_path IS NOT NULL "
            "AND turn_count - COALESCE(last_digested_pair_index, 0) >= %s "
            "ORDER BY updated_at DESC LIMIT %s",
            (threshold, max_sessions),
        )
        if not rows:
            return

        from gobby.memory.digest import build_turn_and_digest

        for row in rows:
            session_id = str(row["id"])
            for _ in range(max_batches_per_session):
                if not self._running:
                    return
                result = await build_turn_and_digest(
                    memory_manager=memory_manager,
                    session_manager=self.session_manager,
                    session_id=session_id,
                    llm_service=llm_service,
                    db=self.db,
                    config=config,
                    catch_up=True,
                )
                if result is None or "error" in result:
                    break
