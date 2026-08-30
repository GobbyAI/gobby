"""Static registry of provider-specific transcript watchdog readers."""

from gobby.agents.watchdog.agy import AGY_WATCHDOG_READER
from gobby.agents.watchdog.claude import CLAUDE_WATCHDOG_READER
from gobby.agents.watchdog.codex import CODEX_WATCHDOG_READER
from gobby.agents.watchdog.droid import DROID_WATCHDOG_READER
from gobby.agents.watchdog.grok import GROK_WATCHDOG_READER
from gobby.agents.watchdog.models import KNOWN_WATCHDOG_PROVIDERS
from gobby.agents.watchdog.qwen import QWEN_WATCHDOG_READER
from gobby.agents.watchdog.reader import TranscriptWatchdogReader

_READERS: dict[str, TranscriptWatchdogReader] = {
    "agy": AGY_WATCHDOG_READER,
    "claude": CLAUDE_WATCHDOG_READER,
    "codex": CODEX_WATCHDOG_READER,
    "droid": DROID_WATCHDOG_READER,
    "grok": GROK_WATCHDOG_READER,
    "qwen": QWEN_WATCHDOG_READER,
}

if frozenset(_READERS) != KNOWN_WATCHDOG_PROVIDERS:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "Watchdog reader registry keys must match KNOWN_WATCHDOG_PROVIDERS: "
        f"{sorted(_READERS)} != {sorted(KNOWN_WATCHDOG_PROVIDERS)}"
    )


class WatchdogReaderRegistry:
    """Resolve a provider-specific watchdog reader without transcript coupling."""

    def for_provider(self, provider_id: str) -> TranscriptWatchdogReader | None:
        if not isinstance(provider_id, str):
            return None
        return _READERS.get(provider_id.strip().lower())
