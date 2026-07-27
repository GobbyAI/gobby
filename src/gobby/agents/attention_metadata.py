from __future__ import annotations

import logging
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from gobby.storage.attention import AttentionOrderingCoordinator

logger = logging.getLogger(__name__)

MAX_METADATA_TEXT_CHARS = 120
MAX_METADATA_TTL_MS = 600_000

AttentionMetadataPublisher = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class _MetadataEntry:
    text: str
    deadline: float


def validate_metadata_text(value: object) -> str:
    """Validate one bounded, UTF-8-safe transient display string."""
    if not isinstance(value, str) or not value or len(value) > MAX_METADATA_TEXT_CHARS:
        raise ValueError(f"text must contain 1-{MAX_METADATA_TEXT_CHARS} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("text must be valid UTF-8") from exc
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("text must not contain control characters")
    return value


def validate_metadata_ttl_ms(value: object) -> int:
    """Validate the producer-facing transient lifetime."""
    if type(value) is not int or not 1 <= value <= MAX_METADATA_TTL_MS:
        raise ValueError(f"ttl_ms must be an integer from 1 to {MAX_METADATA_TTL_MS}")
    return value


class AttentionMetadataStore:
    """Keep cursor-ordered transient agent metadata on a monotonic clock."""

    def __init__(
        self,
        ordering: AttentionOrderingCoordinator,
        *,
        event_publisher: AttentionMetadataPublisher | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.ordering = ordering
        self._event_publisher = event_publisher
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._entries: dict[str, _MetadataEntry] = {}

    def set(self, entry_id: str, text: object, ttl_ms: object) -> dict[str, str]:
        """Set one chip and immediately publish its cursor-ordered snapshot."""
        if not isinstance(entry_id, str) or not entry_id:
            raise ValueError("entry_id is required")
        validated_text = validate_metadata_text(text)
        validated_ttl = validate_metadata_ttl_ms(ttl_ms)

        with self.ordering.synchronized():
            monotonic_now = self._monotonic_clock()
            wall_now = self._wall_clock()
            entry = _MetadataEntry(
                text=validated_text,
                deadline=monotonic_now + validated_ttl / 1_000,
            )
            self._entries[entry_id] = entry
            metadata = self._serialize(entry, monotonic_now=monotonic_now, wall_now=wall_now)
            event = {
                "entry_id": entry_id,
                "epoch": self.ordering.epoch,
                "seq": self.ordering.next_seq(),
                "metadata": metadata,
            }
            self._publish(event)
            return metadata

    def get(self, entry_id: str) -> dict[str, str] | None:
        """Return one unexpired serialized chip, lazily sweeping stale state."""
        with self.ordering.synchronized():
            monotonic_now = self._monotonic_clock()
            entry = self._entries.get(entry_id)
            if entry is None:
                return None
            if entry.deadline <= monotonic_now:
                del self._entries[entry_id]
                return None
            return self._serialize(
                entry,
                monotonic_now=monotonic_now,
                wall_now=self._wall_clock(),
            )

    def clear(self, entry_id: str) -> bool:
        """Remove one chip and publish its cursor-ordered deletion."""
        if not isinstance(entry_id, str) or not entry_id:
            raise ValueError("entry_id is required")

        with self.ordering.synchronized():
            if self._entries.pop(entry_id, None) is None:
                return False
            self._publish(
                {
                    "entry_id": entry_id,
                    "epoch": self.ordering.epoch,
                    "seq": self.ordering.next_seq(),
                    "metadata": None,
                }
            )
            return True

    def snapshot(self) -> Mapping[str, Mapping[str, object]]:
        """Return an immutable-by-copy view of all unexpired metadata."""
        with self.ordering.synchronized():
            monotonic_now = self._monotonic_clock()
            wall_now = self._wall_clock()
            expired = [
                entry_id
                for entry_id, entry in self._entries.items()
                if entry.deadline <= monotonic_now
            ]
            for entry_id in expired:
                del self._entries[entry_id]
            return {
                entry_id: self._serialize(
                    entry,
                    monotonic_now=monotonic_now,
                    wall_now=wall_now,
                )
                for entry_id, entry in self._entries.items()
            }

    @staticmethod
    def _serialize(
        entry: _MetadataEntry,
        *,
        monotonic_now: float,
        wall_now: datetime,
    ) -> dict[str, str]:
        remaining = max(0.0, entry.deadline - monotonic_now)
        expires_at = wall_now + timedelta(seconds=remaining)
        return {"text": entry.text, "expires_at": expires_at.isoformat()}

    def _publish(self, event: dict[str, object]) -> None:
        if self._event_publisher is None:
            return
        try:
            self._event_publisher(event)
        except Exception:
            logger.warning("Failed to publish attention metadata event", exc_info=True)
