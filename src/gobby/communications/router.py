"""Message router for communication events."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.communications.models import CommsRoutingRule
    from gobby.storage.communications import LocalCommunicationsStore


logger = logging.getLogger(__name__)


class MessageRouter:
    """Routes events to communication channels based on rules."""

    def __init__(self, store: LocalCommunicationsStore) -> None:
        """Initialize with storage.

        Args:
            store: Communications storage manager.
        """
        self.store = store
        self._rules_cache: list[CommsRoutingRule] | None = None
        self._cache_expires_at: float = 0
        self._cache_ttl: float = 30.0  # seconds

    def invalidate_cache(self) -> None:
        """Invalidate the routing rules cache."""
        self._rules_cache = None
        self._cache_expires_at = 0

    async def match_channels(
        self,
        event_type: str,
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        """Match event to enabled channels based on routing rules.

        Args:
            event_type: Type of event (e.g., "task.created").
            project_id: Optional project ID to filter rules.
            session_id: Optional session ID to filter rules.

        Returns:
            List of matched channel IDs, sorted by priority (highest first).
        """
        now = time.monotonic()
        if self._rules_cache is None or now >= self._cache_expires_at:
            # rules should already be sorted by priority from store
            rules = await asyncio.to_thread(self.store.list_routing_rules, enabled_only=True)
            # Handle both sync and async return from store if needed in future
            if asyncio.iscoroutine(rules):
                rules = await rules
            self._rules_cache = rules
            self._cache_expires_at = now + self._cache_ttl
        else:
            rules = self._rules_cache

        event_type = self._normalize_event_type(event_type)

        matched_channel_ids: list[str] = []
        seen_channels: set[str] = set()

        for rule in rules:
            # Match project_id if specified in rule
            if rule.project_id and rule.project_id != project_id:
                continue

            # Match session_id if specified in rule
            if rule.session_id and rule.session_id != session_id:
                continue

            # Match event pattern
            if self._matches_pattern(event_type, rule.event_pattern):
                if rule.channel_id and rule.channel_id not in seen_channels:
                    matched_channel_ids.append(rule.channel_id)
                    seen_channels.add(rule.channel_id)

        return matched_channel_ids

    def _matches_pattern(self, event_type: str, pattern: str) -> bool:
        """Check if event type matches a glob pattern.

        Args:
            event_type: Normalized event type.
            pattern: Glob pattern (e.g., "task.*").

        Returns:
            True if matched.
        """
        return fnmatch.fnmatch(event_type, pattern)

    def _normalize_event_type(self, event_type: str) -> str:
        """Normalize event type to dot-separated format.

        Args:
            event_type: Input event type.

        Returns:
            Normalized event type.
        """
        # Replace colons, slashes, or spaces with dots
        normalized = event_type.replace(":", ".").replace("/", ".").replace(" ", ".")
        return normalized.lower()
