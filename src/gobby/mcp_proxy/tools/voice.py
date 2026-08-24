"""
Internal MCP tools for Whisper custom vocabulary management.

Exposes functionality for:
- add_vocab(terms): Add terms to Whisper vocabulary (comma-separated, deduped)
- remove_vocab(terms): Remove terms from Whisper vocabulary (case-insensitive)
- list_vocab(): List current vocabulary and whisper_prompt
- clear_vocab(): Clear all vocabulary terms
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from gobby.config.runtime import ConfigSnapshot
from gobby.config.values import ConfigRuntimeReader, ConfigValuesError
from gobby.mcp_proxy.tools.internal import InternalToolRegistry

logger = logging.getLogger(__name__)

__all__ = ["create_voice_registry"]


class VoiceConfigService(Protocol):
    @property
    def runtime(self) -> ConfigRuntimeReader: ...

    async def patch_flat(
        self,
        *,
        expected_revision: int,
        values: Mapping[str, object],
    ) -> dict[str, object]: ...


def _snapshot(service: VoiceConfigService) -> ConfigSnapshot:
    try:
        return service.runtime.snapshot
    except RuntimeError as exc:
        raise ConfigValuesError(
            "runtime_unavailable",
            "Configuration runtime is not ready",
            (),
            status_code=503,
            retryable=True,
        ) from exc


def create_voice_registry(
    config_service_getter: Callable[[], VoiceConfigService],
) -> InternalToolRegistry:
    """Create a voice tool registry backed by typed revisioned config."""
    registry = InternalToolRegistry(
        name="gobby-voice",
        description="Whisper custom vocabulary - add_vocab, remove_vocab, list_vocab, clear_vocab",
    )

    @registry.tool(
        name="add_vocab",
        description="Add terms to Whisper STT vocabulary. Comma-separated, deduplicates case-insensitively. Example: add_vocab(terms='Kubernetes, FastAPI')",
    )
    async def add_vocab(terms: str) -> dict[str, Any]:
        """Add one or more terms to the vocabulary."""
        service = config_service_getter()
        new_terms = [t.strip() for t in terms.split(",") if t.strip()]
        if not new_terms:
            return {"success": False, "error": "No valid terms provided"}

        try:
            snapshot = _snapshot(service)
            # RMW must base on desired values: active lags behind pending
            # desired writes and would silently revert them.
            current = list(snapshot.desired.voice.whisper_vocabulary)
            existing_lower = {t.lower() for t in current}
            added = []
            for term in new_terms:
                if term.lower() not in existing_lower:
                    current.append(term)
                    existing_lower.add(term.lower())
                    added.append(term)

            if added:
                await service.patch_flat(
                    expected_revision=snapshot.revision,
                    values={"voice.whisper_vocabulary": current},
                )
        except ConfigValuesError as exc:
            return exc.public_body()

        return {
            "success": True,
            "added": added,
            "already_existed": len(new_terms) - len(added),
            "total": len(current),
        }

    @registry.tool(
        name="remove_vocab",
        description="Remove terms from Whisper STT vocabulary. Comma-separated, case-insensitive matching.",
    )
    async def remove_vocab(terms: str) -> dict[str, Any]:
        """Remove one or more terms from the vocabulary."""
        service = config_service_getter()
        to_remove = {t.strip().lower() for t in terms.split(",") if t.strip()}
        if not to_remove:
            return {"success": False, "error": "No valid terms provided"}

        try:
            snapshot = _snapshot(service)
            current = list(snapshot.desired.voice.whisper_vocabulary)
            original_count = len(current)
            remaining = [t for t in current if t.lower() not in to_remove]
            removed_count = original_count - len(remaining)

            if removed_count > 0:
                await service.patch_flat(
                    expected_revision=snapshot.revision,
                    values={"voice.whisper_vocabulary": remaining},
                )
        except ConfigValuesError as exc:
            return exc.public_body()

        return {
            "success": True,
            "removed": removed_count,
            "not_found": len(to_remove) - removed_count,
            "total": len(remaining),
        }

    @registry.tool(
        name="list_vocab",
        description="List current Whisper STT vocabulary terms and prompt.",
    )
    def list_vocab() -> dict[str, Any]:
        """List the current vocabulary and whisper_prompt."""
        try:
            config = _snapshot(config_service_getter()).active.voice
        except ConfigValuesError as exc:
            return exc.public_body()
        vocab = list(config.whisper_vocabulary)
        return {
            "success": True,
            "vocabulary": vocab,
            "count": len(vocab),
            "whisper_prompt": config.whisper_prompt,
        }

    @registry.tool(
        name="clear_vocab",
        description="Clear all Whisper STT vocabulary terms.",
    )
    async def clear_vocab() -> dict[str, Any]:
        """Clear all vocabulary terms."""
        service = config_service_getter()
        try:
            snapshot = _snapshot(service)
            current_count = len(snapshot.desired.voice.whisper_vocabulary)
            await service.patch_flat(
                expected_revision=snapshot.revision,
                values={"voice.whisper_vocabulary": []},
            )
        except ConfigValuesError as exc:
            return exc.public_body()
        return {
            "success": True,
            "cleared": current_count,
        }

    return registry
