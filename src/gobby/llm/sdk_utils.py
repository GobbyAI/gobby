"""Shared utilities for Claude Agent SDK integration.

Functions extracted from claude_streaming.py, chat_session.py, and
chat_session_helpers.py to eliminate duplication across SDK consumers.
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import Any

from gobby.utils.native_bin import resolve_native_bin

logger = logging.getLogger(__name__)


def sanitize_error(e: Exception) -> str:
    """Return a user-facing error message, hiding internal library details."""
    msg = str(e)
    if "model isn't mapped" in msg or "custom_llm_provider" in msg:
        return "An internal error occurred. Please try again."
    return msg


def parse_server_name(full_tool_name: str) -> str:
    """Extract server name from mcp__{server}__{tool} format."""
    if full_tool_name.startswith("mcp__"):
        parts = full_tool_name.split("__")
        if len(parts) >= 2:
            return parts[1]
    return "builtin"


def format_exception_group(eg: ExceptionGroup) -> str:
    """Format an ExceptionGroup into a semicolon-separated error string."""
    errors = [sanitize_error(exc) for exc in eg.exceptions]
    return "; ".join(errors)


# Claude Code / Agent SDK hard-truncates additionalContext at 10K chars.
# We cap slightly below to avoid the ugly "... [output truncated]" suffix.
ADDITIONAL_CONTEXT_LIMIT = 9_950


def truncate_additional_context(text: str) -> str:
    """Truncate text to fit within the SDK's additionalContext limit."""
    if len(text) <= ADDITIONAL_CONTEXT_LIMIT:
        return text
    return text[: ADDITIONAL_CONTEXT_LIMIT - 16] + "\n... [truncated]"


# Minimum text length worth compressing — subprocess overhead isn't justified below this.
_MIN_COMPRESS_LENGTH = 500

# Pattern for parsing gsqz --stats stderr output.
_STATS_RE = re.compile(
    r"\[gsqz\]\s+strategy=(?P<strategy>\S+)\s+"
    r"original=(?P<original>\d+)\s+"
    r"compressed=(?P<compressed>\d+)\s+"
    r"savings=(?P<savings>[\d.]+)%"
)


def _resolve_gsqz_bin() -> str | None:
    """Resolve the gsqz binary path. Returns None if not found."""
    return resolve_native_bin("gsqz")


def compress_context(
    text: str,
    level: str = "standard",
    gsqz_bin: str | None = None,
    enabled: bool = True,
) -> tuple[str, dict[str, Any] | None]:
    """Compress prose content via ``gsqz input`` before context injection.

    This is **injection-time only** — never used on stored content (memories,
    embeddings, FTS indexes).  Fail-open: any error returns the original text.

    Args:
        text: Raw context text to compress.
        level: Compression aggressiveness (``lite``, ``standard``, ``aggressive``).
        gsqz_bin: Override gsqz binary path (for testing). Resolved automatically
            if *None*.
        enabled: Master switch — pass *False* to skip compression entirely.

    Returns:
        ``(compressed_text, stats_dict_or_none)`` where *stats_dict* has keys
        ``original_chars``, ``compressed_chars``, ``savings_pct``, ``strategy``
        when compression ran, or *None* when skipped/failed.
    """
    if not enabled or len(text) < _MIN_COMPRESS_LENGTH:
        return text, None

    binary = gsqz_bin or _resolve_gsqz_bin()
    if binary is None:
        logger.debug("gsqz binary not found — skipping context compression")
        return text, None

    if level not in ("lite", "standard", "aggressive"):
        logger.warning(f"Invalid compression level {level!r}, falling back to 'standard'")
        level = "standard"

    try:
        proc = subprocess.run(
            [binary, "input", "--level", level, "--stats"],
            input=text,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning(f"gsqz input failed: {e}")
        return text, None

    if proc.returncode != 0:
        logger.warning(f"gsqz input exited {proc.returncode}: {proc.stderr[:200]}")
        return text, None

    compressed = proc.stdout
    if not compressed or not compressed.strip():
        return text, None

    # Parse stats from stderr
    stats: dict[str, Any] | None = None
    if proc.stderr:
        m = _STATS_RE.search(proc.stderr)
        if m:
            stats = {
                "strategy": m.group("strategy"),
                "original_chars": int(m.group("original")),
                "compressed_chars": int(m.group("compressed")),
                "savings_pct": float(m.group("savings")),
            }

    return compressed, stats


def _get_compression_config() -> tuple[bool, str]:
    """Read compression config from app context. Returns (enabled, level)."""
    try:
        from gobby.app_context import get_app_context

        ctx = get_app_context()
        if ctx is not None:
            cc = getattr(ctx.config, "context_compression", None)
            if cc is not None:
                return cc.enabled, cc.level
    except (ImportError, AttributeError, TypeError):
        # Fail-open to defaults if app context isn't wired up (e.g. in tests
        # or one-shot scripts that import this module without a daemon).
        pass
    return True, "standard"


def compress_and_truncate(text: str) -> tuple[str, dict[str, Any] | None]:
    """Compress prose content via gsqz input, then truncate to the SDK limit.

    Reads compression config from the app context (fail-open defaults if
    unavailable).  Records savings via :func:`gobby.savings.record.record_savings`.

    This is the standard chokepoint for all adapters — call this instead of
    ``truncate_additional_context`` directly to get compression for free.

    Returns:
        ``(truncated_text, stats_or_none)``
    """
    enabled, level = _get_compression_config()
    compressed, stats = compress_context(text, level=level, enabled=enabled)

    # Record savings (best-effort, never raises)
    if stats:
        try:
            from gobby.savings.record import record_savings

            record_savings(
                category="compression",
                original_chars=stats.get("original_chars", 0),
                actual_chars=stats.get("compressed_chars", 0),
                metadata={"strategy": stats.get("strategy", "prose")},
            )
        except Exception:
            pass

    return truncate_additional_context(compressed), stats
