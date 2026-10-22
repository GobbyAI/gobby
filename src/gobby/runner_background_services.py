"""Startup and shutdown of shared transcript-processing workers."""

from __future__ import annotations

import logging

from gobby.runner_lifecycle_startup import StartupTracker, timed_startup_phase

logger = logging.getLogger("gobby.runner_lifecycle")


async def start_background_services(tracker: StartupTracker | None) -> None:
    """Prewarm workers after HTTP and WebSocket admission is available."""
    from gobby.tasks.transcript_evidence_pool import prewarm_transcript_evidence_pool

    try:
        await timed_startup_phase("transcript_pool_prewarm", prewarm_transcript_evidence_pool())
    except Exception as exc:
        logger.exception("Transcript evidence pool prewarm failed during startup")
        if tracker:
            tracker.error("Transcript evidence pool", str(exc))


def shutdown_background_services() -> None:
    """Stop workers within the caller's existing shutdown ordering and error boundary."""
    from gobby.tasks.transcript_evidence_pool import shutdown_transcript_evidence_pool

    shutdown_transcript_evidence_pool()
