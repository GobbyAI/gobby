"""Provider-bound access to the shared detection manifest registry."""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from gobby.agents.detection.matcher import CompiledManifest

logger = logging.getLogger(__name__)


class DetectionRegistry(Protocol):
    """Minimal registry surface consumed by pane detectors."""

    def for_provider(self, provider_id: str) -> CompiledManifest | None: ...


_missing_provider_lock = threading.Lock()
_logged_missing_providers: set[str] = set()


def resolve_manifest(
    registry: DetectionRegistry,
    provider_id: str,
) -> CompiledManifest | None:
    """Resolve one provider and warn once process-wide when unsupported."""

    normalized = provider_id.strip().lower()
    manifest = registry.for_provider(normalized)
    if manifest is not None:
        return manifest

    with _missing_provider_lock:
        if normalized in _logged_missing_providers:
            return None
        _logged_missing_providers.add(normalized)
    logger.warning("No detection manifest for provider %s", normalized)
    return None
