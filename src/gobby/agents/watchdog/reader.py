"""Reader contract for provider-neutral transcript watchdog signals."""

from typing import Protocol

from gobby.agents.watchdog.models import WatchdogTranscriptSnapshot


class TranscriptWatchdogReader(Protocol):
    provider_id: str
    capacity_pane_message: str | None
    supports_reasoning_interrupt: bool

    async def read(self, transcript_path: str) -> WatchdogTranscriptSnapshot: ...
