"""Provider-neutral transcript signals for the agent idle watchdog."""

from gobby.agents.watchdog.models import (
    WATCHDOG_TAIL_LIMIT,
    CapacityRecoveryState,
    TranscriptEventSummary,
    WatchdogTranscriptSnapshot,
)
from gobby.agents.watchdog.reader import TranscriptWatchdogReader
from gobby.agents.watchdog.registry import WatchdogReaderRegistry

__all__ = [
    "WATCHDOG_TAIL_LIMIT",
    "CapacityRecoveryState",
    "TranscriptEventSummary",
    "TranscriptWatchdogReader",
    "WatchdogReaderRegistry",
    "WatchdogTranscriptSnapshot",
]
