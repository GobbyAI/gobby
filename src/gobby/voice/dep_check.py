"""Auto-install voice dependencies when enabled but missing.

Voice packages (faster-whisper, chatterbox, etc.) are optional extras.
When voice is enabled in config but packages are absent, this module
installs them via ``uv pip install`` and invalidates import caches so
the running process can import them immediately.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from typing import TYPE_CHECKING

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
    "kokoro": [
        ("kokoro-onnx", "kokoro_onnx"),
    ],
}

# Guard against concurrent install attempts
_install_lock = asyncio.Lock()


def _check_imports(deps: list[tuple[str, str]]) -> list[str]:
    """Return pip package names for deps that fail to import."""
    missing = []
    for pip_name, import_name in deps:
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pip_name)
    return missing


async def _install_packages(packages: list[str]) -> bool:
    """Install packages via uv pip install. Returns True on success."""
    logger.info(f"Installing voice packages: {', '.join(packages)}")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uv",
        "pip",
        "install",
        *packages,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode == 0:
        importlib.invalidate_caches()
        logger.info("Voice packages installed successfully")
        return True

    logger.error(
        f"Failed to install voice packages (exit {proc.returncode}): "
        f"{stderr.decode(errors='replace').strip()}"
    )
    return False


async def ensure_stt_deps(config: VoiceConfig) -> bool:
    """Ensure STT dependencies are available. Auto-installs if missing.

    Returns True if all STT deps are importable after this call.
    """
    if not config.enabled or not config.stt_enabled:
        return False

    missing = _check_imports(_STT_DEPS)
    if not missing:
        return True

    async with _install_lock:
        # Re-check after acquiring lock (another coroutine may have installed)
        missing = _check_imports(_STT_DEPS)
        if not missing:
            return True
        return await _install_packages(missing)


async def ensure_tts_deps(config: VoiceConfig) -> bool:
    """Ensure TTS dependencies are available. Auto-installs if missing.

    Returns True if all TTS deps for the configured provider are importable.
    """
    if not config.enabled or not config.tts_enabled:
        return False

    provider = config.tts_provider
    deps = _TTS_DEPS.get(provider, [])
    if not deps:
        logger.warning(f"Unknown TTS provider: {provider}")
        return False

    missing = _check_imports(deps)
    if not missing:
        return True

    async with _install_lock:
        missing = _check_imports(deps)
        if not missing:
            return True
        return await _install_packages(missing)
