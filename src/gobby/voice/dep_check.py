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
import shutil
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
    "voxcpm": [
        ("voxcpm", "voxcpm"),
    ],
}

_AUTO_INSTALL_TTS_PROVIDERS = {"chatterbox", "kokoro"}

# Guard against concurrent install attempts
_install_lock = asyncio.Lock()

# Maximum time to wait for `uv pip install` to finish before killing it.
# Voice deps include large wheels (PyTorch, faster-whisper, chatterbox), so
# the timeout is generous — but bounded so a hung install doesn't wedge the
# daemon forever.
VOICE_PIP_TIMEOUT_SECONDS = 600.0


def _check_imports(deps: list[tuple[str, str]]) -> list[str]:
    """Return pip package names for deps that fail to import."""
    missing = []
    for pip_name, import_name in deps:
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pip_name)
    return missing


def _resolve_uv_install_command() -> list[str] | None:
    """Return a uv command that installs into the current interpreter's env."""
    uv_bin = shutil.which("uv")
    if uv_bin:
        return [uv_bin, "pip", "install", "--python", sys.executable]

    if importlib.util.find_spec("uv") is not None:
        return [sys.executable, "-m", "uv", "pip", "install", "--python", sys.executable]

    return None


async def _install_packages(packages: list[str]) -> bool:
    """Install packages via uv pip install. Returns True on success.

    Bounded by ``VOICE_PIP_TIMEOUT_SECONDS`` — if the install hangs, the
    subprocess is killed and reaped, and the function returns False.
    """
    command = _resolve_uv_install_command()
    if command is None:
        logger.error(
            "Failed to install voice packages: uv is not available as a binary "
            "and not importable in %s",
            sys.executable,
        )
        return False

    logger.info(f"Installing voice packages: {', '.join(packages)}")
    proc = await asyncio.create_subprocess_exec(
        *command,
        *packages,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=VOICE_PIP_TIMEOUT_SECONDS
        )
    except TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        logger.error(
            "Voice package install timed out after %.0fs (packages: %s) — killed",
            VOICE_PIP_TIMEOUT_SECONDS,
            ", ".join(packages),
        )
        return False

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

    if provider not in _AUTO_INSTALL_TTS_PROVIDERS:
        logger.info(
            "Skipping auto-install for TTS provider '%s'; install dependencies manually",
            provider,
        )
        return False

    async with _install_lock:
        missing = _check_imports(deps)
        if not missing:
            return True
        return await _install_packages(missing)
