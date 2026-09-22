"""Shared terminal activity capture for wake dispatch and restart recovery."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.agents.idle_detector import COMPOSER_PROBE_LINES, ComposerRead, IdleDetector
from gobby.events.live_wake import TerminalActivity

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)


async def probe_terminal_activity(
    runner: GobbyRunner,
    session: Any,
    terminal: Any | None,
) -> TerminalActivity:
    """Classify composer and in-flight evidence from one ANSI snapshot."""
    from gobby.sessions.tmux_context import parse_terminal_context_value
    from gobby.terminals.lookup import manager_for_terminal_context
    from gobby.terminals.pane_io import TmuxPaneIO

    unknown = TerminalActivity(ComposerRead("unknown"))
    source = getattr(session, "source", None)
    registry = getattr(runner, "detection_registry", None)
    if not source or registry is None:
        return unknown
    try:
        if terminal is not None:
            services = runner.terminal_services
            if services is None:
                return unknown
            result = await services.runtime_for(terminal).snapshot(
                terminal,
                COMPOSER_PROBE_LINES,
                mode="ansi",
            )
            text: str | None = result.text
        else:
            ctx = parse_terminal_context_value(getattr(session, "terminal_context", None))
            target = ctx.get("tmux_pane") if ctx else None
            if not target:
                return unknown
            text = await TmuxPaneIO(manager_for_terminal_context(ctx), str(target)).snapshot(
                COMPOSER_PROBE_LINES,
                mode="ansi",
            )
        detector = IdleDetector(registry, str(source))
        return TerminalActivity(
            composer=detector.composer_read(text),
            turn_in_flight_fingerprint=detector.turn_in_flight_fingerprint(text or ""),
        )
    except Exception:
        logger.debug(
            "Terminal activity probe failed for session %s",
            getattr(session, "id", None),
            exc_info=True,
        )
        return unknown
