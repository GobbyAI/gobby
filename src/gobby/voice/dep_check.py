"""Runtime health checks for required local voice dependencies."""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

from gobby.voice._warnings import suppress_perth_pkg_resources_warning

if TYPE_CHECKING:
    from gobby.config.voice import VoiceConfig

logger = logging.getLogger(__name__)

# (pip package name, Python import name)
_STT_DEPS: list[tuple[str, str]] = [
    ("faster-whisper", "faster_whisper"),
]

_TTS_DEPS: dict[str, list[tuple[str, str]]] = {
    "chatterbox": [
        ("chatterbox-tts", "chatterbox"),
    ],
}


def _check_imports(deps: list[tuple[str, str]]) -> list[str]:
    """Return pip package names for deps that fail to import."""
    missing = []
    for pip_name, import_name in deps:
        try:
            with suppress_perth_pkg_resources_warning():
                importlib.import_module(import_name)
        except ImportError:
            missing.append(pip_name)
    return missing


async def ensure_stt_deps(config: VoiceConfig) -> bool:
    """Return True when required STT dependencies are importable.

    Missing packages indicate a broken daemon environment; run ``uv sync`` to repair it.
    """
    if not config.enabled or not config.stt_enabled:
        return False

    missing = _check_imports(_STT_DEPS)
    if missing:
        logger.error(
            "Daemon environment is missing required STT package(s): %s; run uv sync",
            ", ".join(missing),
        )
        return False
    return True


async def ensure_tts_deps(config: VoiceConfig) -> bool:
    """Return True when required TTS dependencies are importable.

    Missing packages indicate a broken daemon environment; run ``uv sync`` to repair it.
    """
    if not config.enabled or not config.tts_enabled:
        return False

    provider = config.tts_provider
    deps = _TTS_DEPS.get(provider, [])
    if not deps:
        logger.warning(f"Unknown TTS provider: {provider}")
        return False

    missing = _check_imports(deps)
    if missing:
        logger.error(
            "Daemon environment is missing required TTS package(s): %s; run uv sync",
            ", ".join(missing),
        )
        return False
    return True
