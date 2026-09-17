"""Composer probe for watchdog injections into a managed agent terminal."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gobby.agents.idle_detector import COMPOSER_PROBE_LINES, IdleDetector

if TYPE_CHECKING:
    from gobby.storage.agents import AgentRun
    from gobby.terminals.services import TerminalServices

logger = logging.getLogger(__name__)


async def composer_holds_draft(
    services: TerminalServices | None,
    idle_detector: IdleDetector,
    run: AgentRun,
) -> bool:
    """Positive read of an operator draft in the agent's composer; errors read False."""
    if services is None:
        return False
    try:
        snapshot = await services.snapshot(run, COMPOSER_PROBE_LINES, mode="ansi")
        text = None if snapshot is None else snapshot.text
        read = idle_detector.for_provider(run.provider).composer_read(text)
    except Exception:
        logger.debug("Composer probe failed for agent %s", run.id, exc_info=True)
        return False
    return read.state == "draft"
