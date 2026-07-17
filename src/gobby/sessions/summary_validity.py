"""Validity checks for persisted session summaries."""

from __future__ import annotations

import re

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
    "current state",
    "next steps",
)
REQUIRED_SUMMARY_HEADINGS: tuple[str, ...] = (
    "## Current State",
    "## Next Steps",
)

_HEADING_PREFIX_RE = re.compile(r"^#{1,6}\s*")
_EMPHASIS_MARKERS = ("**", "__", "*", "_")


def is_summary_failure_sentinel(summary_markdown: str | None) -> bool:
    """Return true when summary text is a provider failure sentinel."""
    if not isinstance(summary_markdown, str) or not summary_markdown:
        return False

    normalized = summary_markdown.strip().lower()
    return any(normalized.startswith(prefix) for prefix in SUMMARY_FAILURE_SENTINEL_PREFIXES)


def is_summary_markdown_valid(summary_markdown: str | None) -> bool:
    """Return true when summary text is substantive and structurally complete."""
    return summary_markdown_validation_error(summary_markdown) is None


def summary_markdown_validation_error(summary_markdown: str | None) -> str | None:
    """Return a bounded reason when summary text is invalid."""
    if not isinstance(summary_markdown, str):
        return "summary must be text"

    stripped = summary_markdown.strip()
    if is_summary_failure_sentinel(stripped):
        return "summary begins with a provider failure sentinel"
    if len(stripped) < MIN_SUMMARY_LENGTH:
        return f"summary is shorter than {MIN_SUMMARY_LENGTH} characters"

    headings = {_semantic_heading(line) for line in stripped.splitlines()}
    missing = [marker.title() for marker in REQUIRED_SUMMARY_MARKERS if marker not in headings]
    if missing:
        return f"summary is missing required section(s): {', '.join(missing)}"
    return None


def summary_prompt_validation_error(prompt_template: str | None) -> str | None:
    """Return an actionable error when a summary prompt omits its output contract."""
    if not isinstance(prompt_template, str) or not prompt_template.strip():
        return "summary prompt template is empty"

    missing = [heading for heading in REQUIRED_SUMMARY_HEADINGS if heading not in prompt_template]
    if missing:
        return f"summary prompt must include literal required heading(s): {', '.join(missing)}"
    return None


def _semantic_heading(line: str) -> str | None:
    """Normalize tolerated Markdown heading variants to their semantic label.

    Returns None for lines with no Markdown heading structure (ATX prefix or
    full emphasis wrapping), so plain prose never satisfies a section check.
    """
    stripped = line.strip()
    prefix_match = _HEADING_PREFIX_RE.match(stripped)
    candidate = stripped[prefix_match.end() :].strip() if prefix_match else stripped
    candidate = candidate.removesuffix(":").strip()

    emphasized = False
    for marker in _EMPHASIS_MARKERS:
        if (
            len(candidate) > 2 * len(marker)
            and candidate.startswith(marker)
            and candidate.endswith(marker)
        ):
            candidate = candidate[len(marker) : -len(marker)].strip()
            emphasized = True
            break

    if prefix_match is None and not emphasized:
        return None
    return candidate.removesuffix(":").strip().casefold()
