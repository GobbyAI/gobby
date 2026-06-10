"""Source-aware session summary refresh helpers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

DIGEST_TURN_PATTERN = re.compile(r"^### Turn \d+.*$", re.MULTILINE)
FULL_REBUILD_DIGEST_TURN_THRESHOLD = 20


@dataclass(frozen=True)
class SummaryRefreshDecision:
    """Decision for a source-aware summary refresh."""

    mode: str
    reason: str
    new_digest_turns: str = ""
    previous_digest_turn_count: int | None = None
    current_digest_turn_count: int = 0


def digest_turns(digest_markdown: str | None) -> list[str]:
    """Split rolling digest markdown into complete turn blocks."""
    if not isinstance(digest_markdown, str) or not digest_markdown.strip():
        return []

    headings = DIGEST_TURN_PATTERN.findall(digest_markdown)
    if not headings:
        return []

    parts = DIGEST_TURN_PATTERN.split(digest_markdown)
    turns: list[str] = []
    for index, heading in enumerate(headings):
        content = parts[index + 1] if index + 1 < len(parts) else ""
        turns.append(f"{heading}\n{content.strip()}".strip())
    return turns


def digest_turn_count(digest_markdown: str | None) -> int:
    """Count structured digest turns."""
    return len(digest_turns(digest_markdown))


def digest_turns_since(digest_markdown: str | None, previous_count: int) -> str:
    """Return digest turns added after a previously persisted watermark."""
    if previous_count < 0:
        return ""
    turns = digest_turns(digest_markdown)
    if previous_count >= len(turns):
        return ""
    return "\n\n".join(turns[previous_count:])


def source_context_hash(payload: dict[str, Any]) -> str:
    """Hash prompt-relevant source context deterministically."""
    encoded = json.dumps(
        payload,
        default=str,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def coerce_digest_turn_count(value: Any) -> int | None:
    """Return a non-negative int watermark from storage metadata."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def choose_summary_refresh(
    *,
    current_source_hash: str,
    current_digest_turn_count: int,
    previous_source_hash: Any,
    previous_digest_turn_count: Any,
    previous_summary_valid: bool,
    digest_markdown: str | None,
) -> SummaryRefreshDecision:
    """Choose no-op, full rebuild, or delta merge for summary refresh."""
    previous_count = coerce_digest_turn_count(previous_digest_turn_count)
    previous_hash = previous_source_hash if isinstance(previous_source_hash, str) else None
    has_metadata = bool(previous_hash) and previous_count is not None

    if has_metadata and previous_hash == current_source_hash and previous_summary_valid:
        return SummaryRefreshDecision(
            mode="noop",
            reason="source_context_hash_match",
            previous_digest_turn_count=previous_count,
            current_digest_turn_count=current_digest_turn_count,
        )

    if not has_metadata:
        return SummaryRefreshDecision(
            mode="full",
            reason="missing_summary_metadata",
            previous_digest_turn_count=previous_count,
            current_digest_turn_count=current_digest_turn_count,
        )

    if not previous_summary_valid:
        return SummaryRefreshDecision(
            mode="full",
            reason="invalid_previous_summary",
            previous_digest_turn_count=previous_count,
            current_digest_turn_count=current_digest_turn_count,
        )

    assert previous_count is not None

    if current_digest_turn_count <= 0:
        return SummaryRefreshDecision(
            mode="full",
            reason="missing_digest_watermark",
            previous_digest_turn_count=previous_count,
            current_digest_turn_count=current_digest_turn_count,
        )

    if current_digest_turn_count < previous_count:
        return SummaryRefreshDecision(
            mode="full",
            reason="digest_watermark_decreased",
            previous_digest_turn_count=previous_count,
            current_digest_turn_count=current_digest_turn_count,
        )

    new_turn_count = current_digest_turn_count - previous_count
    if new_turn_count >= FULL_REBUILD_DIGEST_TURN_THRESHOLD:
        return SummaryRefreshDecision(
            mode="full",
            reason="digest_delta_threshold_reached",
            new_digest_turns=digest_turns_since(digest_markdown, previous_count),
            previous_digest_turn_count=previous_count,
            current_digest_turn_count=current_digest_turn_count,
        )

    return SummaryRefreshDecision(
        mode="delta",
        reason="safe_digest_watermark",
        new_digest_turns=digest_turns_since(digest_markdown, previous_count),
        previous_digest_turn_count=previous_count,
        current_digest_turn_count=current_digest_turn_count,
    )
