"""Validity checks for persisted session summaries."""

from __future__ import annotations

SUMMARY_FAILURE_SENTINEL_PREFIXES: tuple[str, ...] = (
    "as an ai",
    "error:",
    "i am sorry",
    "i cannot",
    "i can't",
    "i’m sorry",
    "i'm sorry",
    "internal server error",
    "session summary generation failed",
    "session summary unavailable",
    "unable to generate",
)
MIN_SUMMARY_LENGTH = 100
REQUIRED_SUMMARY_MARKERS: tuple[str, ...] = (
    "## current state",
    "## next steps",
)


def is_summary_failure_sentinel(summary_markdown: str | None) -> bool:
    """Return true when summary text is a provider failure sentinel."""
    if not isinstance(summary_markdown, str) or not summary_markdown:
        return False

    normalized = summary_markdown.strip().lower()
    return any(normalized.startswith(prefix) for prefix in SUMMARY_FAILURE_SENTINEL_PREFIXES)


def is_summary_markdown_valid(summary_markdown: str | None) -> bool:
    """Return true when summary text is substantive and structurally complete."""
    if not isinstance(summary_markdown, str):
        return False

    stripped = summary_markdown.strip()
    if len(stripped) < MIN_SUMMARY_LENGTH or is_summary_failure_sentinel(stripped):
        return False

    headings = {line.strip().casefold() for line in stripped.splitlines()}
    return all(marker in headings for marker in REQUIRED_SUMMARY_MARKERS)
