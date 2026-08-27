"""Session summary generation scheduling for hook handlers."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from gobby.hooks.background_tasks import create_background_task


class SessionSummaryDispatcher:
    """Schedule session summary generation on the best available event loop."""

    def __init__(
        self,
        *,
        session_manager: Any,
        llm_service: Any,
        session_summary_config: Any | None,
        database: Any,
        loop: asyncio.AbstractEventLoop | None,
        logger: logging.Logger,
        memory_manager: Any | None = None,
        config: Any | None = None,
    ) -> None:
        self.session_manager = session_manager
        self.llm_service = llm_service
        self.session_summary_config = session_summary_config
        self.database = database
        self.loop = loop
        self.logger = logger
        self.memory_manager = memory_manager
        self.config = config

    async def _pre_digest(self, session_id: str) -> bool:
        """Digest pending transcript facts before generating a summary."""
        if self.memory_manager is None or self.llm_service is None:
            return True

        from gobby.memory.digest import build_turn_and_digest

        try:
            outcome = await build_turn_and_digest(
                memory_manager=self.memory_manager,
                session_manager=self.session_manager,
                session_id=session_id,
                llm_service=self.llm_service,
                db=self.database,
                config=self.config,
            )
        except Exception:
            self.logger.warning(
                "pre-summary digest raised for %s",
                session_id,
                exc_info=True,
            )
            return True

        if outcome is None:
            return True
        if outcome.get("error_kind") == "transcript_read":
            self.logger.warning(
                "pre-summary digest hit transcript corruption for %s: %s; refresh aborted",
                session_id,
                outcome,
            )
            return False
        if outcome.get("tail_withheld"):
            self.logger.info(
                "pre-summary digest withheld the in-flight tail for %s; refresh deferred",
                session_id,
            )
            return False
        if "error" in outcome or outcome.get("cancelled"):
            self.logger.warning("pre-summary digest failed for %s: %s", session_id, outcome)
        return True

    def dispatch(
        self,
        session_id: str,
        _background: bool = False,
        done_event: threading.Event | None = None,
        set_handoff_ready: bool = False,
    ) -> None:
        """Fire session summary generation in the background."""
        from gobby.sessions.summarize import generate_session_summaries

        async def _run(*, pre_digest: bool) -> None:
            try:
                if pre_digest:
                    if not await self._pre_digest(session_id):
                        return
                elif self.memory_manager is not None and self.llm_service is not None:
                    self.logger.debug(
                        "pre-summary digest skipped: no daemon loop for %s",
                        session_id,
                    )
                await generate_session_summaries(
                    session_id=session_id,
                    session_manager=self.session_manager,
                    llm_service=self.llm_service,
                    session_summary_config=self.session_summary_config,
                    db=self.database,
                    set_handoff_ready=set_handoff_ready,
                )
            except Exception as exc:
                self.logger.exception(
                    "_dispatch_session_summaries: failed for session %s: %s: %s",
                    session_id,
                    type(exc).__name__,
                    exc,
                )
            finally:
                if done_event:
                    done_event.set()

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        daemon_loop = self.loop
        if daemon_loop is not None and daemon_loop.is_running():
            if running_loop is daemon_loop:
                create_background_task(_run(pre_digest=True), loop=daemon_loop)
                return

            coro = _run(pre_digest=True)
            try:
                asyncio.run_coroutine_threadsafe(coro, daemon_loop)
            except Exception as exc:
                coro.close()
                self.logger.warning("_dispatch_session_summaries: failed to schedule: %s", exc)
                if done_event:
                    done_event.set()
            return

        if running_loop is not None:
            create_background_task(_run(pre_digest=False), loop=running_loop)
            return

        self._dispatch_without_running_loop(_run(pre_digest=False), done_event)

    def _dispatch_without_running_loop(
        self,
        coro: Any,
        done_event: threading.Event | None,
    ) -> None:
        def _run_coro() -> None:
            try:
                asyncio.run(coro)
            except Exception as exc:
                self.logger.warning("_dispatch_session_summaries: background failed: %s", exc)
                if done_event:
                    done_event.set()

        threading.Thread(target=_run_coro, daemon=True).start()
