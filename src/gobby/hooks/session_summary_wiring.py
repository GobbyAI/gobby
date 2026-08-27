"""Construction boundary for hook-triggered session summaries."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gobby.hooks.session_summary_dispatcher import SessionSummaryDispatcher


def build_session_summary_dispatcher(
    *,
    session_manager: Any,
    llm_service: Any,
    session_summary_config: Any | None,
    database: Any,
    loop: asyncio.AbstractEventLoop | None,
    logger: logging.Logger,
    memory_manager: Any | None,
    config: Any | None,
) -> SessionSummaryDispatcher:
    """Build a dispatcher from retained hook-manager dependencies."""
    return SessionSummaryDispatcher(
        session_manager=session_manager,
        llm_service=llm_service,
        session_summary_config=session_summary_config,
        database=database,
        loop=loop,
        logger=logger,
        memory_manager=memory_manager,
        config=config,
    )
