"""Static registry of provider-specific transcript watchdog readers."""

from gobby.agents.watchdog.claude import CLAUDE_WATCHDOG_READER
from gobby.agents.watchdog.codex import CODEX_WATCHDOG_READER
from gobby.agents.watchdog.models import KNOWN_WATCHDOG_PROVIDERS
from gobby.agents.watchdog.reader import TranscriptWatchdogReader

_READERS: dict[str, TranscriptWatchdogReader | None] = {
    "claude": CLAUDE_WATCHDOG_READER,
    "codex": CODEX_WATCHDOG_READER,
    "droid": None,
    "grok": None,
    "qwen": None,
}

assert frozenset(_READERS) == KNOWN_WATCHDOG_PROVIDERS


class WatchdogReaderRegistry:
    """Resolve a provider-specific watchdog reader without transcript coupling."""

    def for_provider(self, provider_id: str) -> TranscriptWatchdogReader | None:
        if not isinstance(provider_id, str):
            return None
        return _READERS.get(provider_id.strip().lower())
