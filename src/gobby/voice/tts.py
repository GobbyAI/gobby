"""Text-to-speech provider abstractions.

Lazy-loads model implementations on first synthesis to avoid slowing daemon
boot. Providers expose a common lifecycle and status surface so the voice
stack does not need provider-specific branching.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from gobby.config.voice import VoiceConfig

logger = logging.getLogger(__name__)


def _module_is_available(module_name: str) -> bool:
    """Check module availability without importing heavyweight runtimes."""
    if module_name in sys.modules:
        return sys.modules[module_name] is not None

    try:
        return importlib.util.find_spec(module_name) is not None
    except (AttributeError, ImportError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class TTSProviderCapabilities:
    """Stable provider capability flags exposed through status responses."""

    supports_reference_audio: bool = False
    supports_reference_text: bool = False
    supports_streaming: bool = False
    supports_voice_cloning: bool = False


@dataclass(frozen=True, slots=True)
class TTSProviderStatus:
    """Availability and metadata for a provider instance."""

    provider: str
    available: bool
    reason: str = ""
    backend_kind: Literal["embedded", "external"] = "embedded"
    capabilities: TTSProviderCapabilities = field(default_factory=TTSProviderCapabilities)
    details: dict[str, Any] = field(default_factory=dict)

    def as_status_fields(self) -> dict[str, Any]:
        """Convert provider status to the public websocket/HTTP status shape."""
        fields: dict[str, Any] = {
            "tts_provider": self.provider,
            "tts_available": self.available,
            "tts_reason": self.reason,
            "tts_backend_kind": self.backend_kind,
            "tts_capabilities": asdict(self.capabilities),
        }
        fields.update(self.details)
        return fields


@runtime_checkable
class TTSProvider(Protocol):
    """Protocol for pluggable TTS engines."""

    provider_name: str
    backend_kind: Literal["embedded", "external"]
    capabilities: TTSProviderCapabilities

    async def warmup(self) -> None:
        """Preload model state needed for synthesis."""
        ...  # pragma: no cover

    def unload(self) -> None:
        """Release provider state and reclaim memory."""
        ...  # pragma: no cover

    def synthesize_stream(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        """Yield ``(pcm_int16_bytes, sample_rate)`` chunks as they are generated."""
        ...  # pragma: no cover

    @property
    def is_available(self) -> bool:
        """Check if the provider is installed and ready to initialize."""
        ...  # pragma: no cover

    @property
    def sample_rate(self) -> int:
        """Output sample rate in Hz."""
        ...  # pragma: no cover

    def get_status(self) -> TTSProviderStatus:
        """Return availability, capabilities, and provider-specific details."""
        ...  # pragma: no cover


class BaseTTSProvider(ABC):
    """Shared provider lifecycle and status helpers."""

    provider_name = "unknown"
    backend_kind: Literal["embedded", "external"] = "embedded"
    capabilities = TTSProviderCapabilities()

    def __init__(self, config: VoiceConfig) -> None:
        self._config = config

    @property
    def is_available(self) -> bool:
        available, _reason = self._availability()
        return available

    def get_status(self) -> TTSProviderStatus:
        available, reason = self._availability()
        return TTSProviderStatus(
            provider=self.provider_name,
            available=available,
            reason=reason,
            backend_kind=self.backend_kind,
            capabilities=self.capabilities,
            details=self._status_details(),
        )

    def _status_details(self) -> dict[str, Any]:
        return {}

    @abstractmethod
    async def warmup(self) -> None:
        """Preload model state needed for synthesis."""
        raise NotImplementedError

    @abstractmethod
    def unload(self) -> None:
        """Release provider state and reclaim memory."""
        raise NotImplementedError

    @abstractmethod
    def synthesize_stream(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        """Yield ``(pcm_int16_bytes, sample_rate)`` chunks as they are generated."""
        raise NotImplementedError

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Output sample rate in Hz."""
        raise NotImplementedError

    @abstractmethod
    def _availability(self) -> tuple[bool, str]:
        """Return ``(available, reason)`` for the provider."""
        raise NotImplementedError
