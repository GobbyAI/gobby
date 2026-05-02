"""Validity checks for persisted session summaries."""

from __future__ import annotations

SUMMARY_FAILURE_SENTINEL_PREFIXES: tuple[str, ...] = (
    "session summary generation failed",
    "session summary unavailable",
)


def is_summary_failure_sentinel(summary_markdown: str | None) -> bool:
    """Return true when summary text is a provider failure sentinel."""
    if not isinstance(summary_markdown, str) or not summary_markdown:
        return False

    normalized = summary_markdown.strip().lower()
    return any(normalized.startswith(prefix) for prefix in SUMMARY_FAILURE_SENTINEL_PREFIXES)


def is_summary_markdown_valid(summary_markdown: str | None) -> bool:
    """Return true when summary text is non-empty and not a failure sentinel."""
    if not isinstance(summary_markdown, str) or not summary_markdown.strip():
        return False
    return not is_summary_failure_sentinel(summary_markdown)
