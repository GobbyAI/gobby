"""CLI detection helpers for install/uninstall commands."""

import shutil
import subprocess


def _is_claude_code_installed() -> bool:
    """Check if Claude Code CLI is installed."""
    return shutil.which("claude") is not None


def _is_gemini_cli_installed() -> bool:
    """Check if Gemini CLI is installed."""
    return shutil.which("gemini") is not None


def _is_qwen_cli_installed() -> bool:
    """Check if Qwen CLI is installed."""
    return shutil.which("qwen") is not None


def _is_codex_cli_installed() -> bool:
    """Check if OpenAI Codex CLI is installed."""
    return shutil.which("codex") is not None


def _is_droid_cli_installed() -> bool:
    """Return True if Factory droid CLI is available in PATH."""
    return shutil.which("droid") is not None


def _is_lmstudio_available() -> bool:
    """Check if LM Studio server is running via `lms server status`."""
    if not shutil.which("lms"):
        return False
    try:
        result = subprocess.run(
            ["lms", "server", "status"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # lms writes the status to stderr
        combined = (result.stdout + result.stderr).lower()
        return result.returncode == 0 and "running" in combined
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _is_ollama_available() -> bool:
    """Check if Ollama is installed and responding."""
    if not shutil.which("ollama"):
        return False
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
