"""Session summary generation scheduling for hook handlers."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any


class SessionSummaryDispatcher:
    """Schedule session summary generation on the best available event loop."""

    def __init__(
        self,
        *,
        session_manager: Any,
        llm_service: Any,
        database: Any,
        loop: asyncio.AbstractEventLoop | None,
        logger: logging.Logger,
    ) -> None:
        self.session_manager = session_manager
        self.llm_service = llm_service
        self.database = database
        self.loop = loop
        self.logger = logger

    def dispatch(
        self,
        session_id: str,
        background: bool = False,
        done_event: threading.Event | None = None,
        set_handoff_ready: bool = False,
    ) -> None:
        """Fire session summary generation in the background."""
        del background

        from gobby.sessions.summarize import generate_session_summaries

        async def _run() -> None:
            try:
                await generate_session_summaries(
                    session_id=session_id,
                    session_manager=self.session_manager,
                    llm_service=self.llm_service,
                    db=self.database,
                    set_handoff_ready=set_handoff_ready,
                )
            except Exception as exc:
                self.logger.error(
                    "_dispatch_session_summaries: failed for session %s: %s: %s",
                    session_id,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
            finally:
                if done_event:
                    done_event.set()

        coro = _run()
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            self._dispatch_without_running_loop(coro, done_event)

    def _dispatch_without_running_loop(
        self,
        coro: Any,
        done_event: threading.Event | None,
    ) -> None:
        if self.loop and self.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(coro, self.loop)
            except Exception as exc:
                coro.close()
                self.logger.warning("_dispatch_session_summaries: failed to schedule: %s", exc)
                if done_event:
                    done_event.set()
            return

        def _run_coro() -> None:
            try:
                asyncio.run(coro)
            except Exception as exc:
                self.logger.warning("_dispatch_session_summaries: background failed: %s", exc)
                if done_event:
                    done_event.set()

        threading.Thread(target=_run_coro, daemon=True).start()
