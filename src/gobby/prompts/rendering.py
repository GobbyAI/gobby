"""Shared safety helpers for rendering LLM prompts."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import escape
from typing import Any

logger = logging.getLogger(__name__)

_INVISIBLE_UNICODE = re.compile(
    r"(?P<tags>[\U000e0000-\U000e007f])"
    r"|(?P<zero_width>[\u200b-\u200d\u2060\ufeff])"
    r"|(?P<bidi>[\u202a-\u202e\u2066-\u2069])"
    r"|(?P<controls>[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f])"
)
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass
class InvisibleCounts:
    """Counts only: never retain the untrusted payload in diagnostics."""

    tags: int = 0
    zero_width: int = 0
    bidi: int = 0
    controls: int = 0

    @property
    def total(self) -> int:
        return self.tags + self.zero_width + self.bidi + self.controls

    def add(self, other: InvisibleCounts) -> None:
        self.tags += other.tags
        self.zero_width += other.zero_width
        self.bidi += other.bidi
        self.controls += other.controls

    def summary(self) -> str:
        return (
            f"tags={self.tags}, zero_width={self.zero_width}, "
            f"bidi={self.bidi}, controls={self.controls}"
        )


def strip_invisible_unicode(text: str) -> tuple[str, InvisibleCounts]:
    """Remove hidden instruction carriers while preserving TAB, LF and CR."""
    counts = InvisibleCounts()

    def remove(match: re.Match[str]) -> str:
        kind = match.lastgroup
        if kind is not None:
            setattr(counts, kind, getattr(counts, kind) + 1)
        return ""

    return _INVISIBLE_UNICODE.sub(remove, text), counts


def clean_untrusted_value(
    value: Any, *, ignore_leading_bom: bool = False, ignore_ansi: bool = False
) -> tuple[Any, InvisibleCounts]:
    """Clean text leaves, preserving keys/binary; inspection may ignore display annotations."""
    if isinstance(value, str):
        if ignore_leading_bom:
            value = value.removeprefix("\ufeff")
        if ignore_ansi:
            value = _ANSI_CSI.sub("", value)
        return strip_invisible_unicode(value)
    counts = InvisibleCounts()
    if isinstance(value, (dict, list, tuple)):
        items = value.items() if isinstance(value, dict) else enumerate(value)
        cleaned: dict[Any, Any] = {}
        for key, item in items:
            cleaned[key], item_counts = clean_untrusted_value(
                item, ignore_leading_bom=ignore_leading_bom, ignore_ansi=ignore_ansi
            )
            counts.add(item_counts)
        if isinstance(value, dict):
            return cleaned, counts
        values = list(cleaned.values())
        return (tuple(values) if isinstance(value, tuple) else values), counts
    return value, counts


def log_invisible_counts(counts: InvisibleCounts, source: str) -> None:
    """Log a fixed boundary label and counts, never payload text or URLs."""
    if counts.total:
        logger.info("Invisible Unicode at %s: %s", source, counts.summary())


def delimit_untrusted_content(value: Any) -> str:
    """Wrap external prompt data in delimiters that the payload cannot close."""
    cleaned, counts = strip_invisible_unicode("" if value is None else str(value))
    log_invisible_counts(counts, "prompt")
    escaped_value = escape(cleaned, quote=False)
    return f"<untrusted_content>\n{escaped_value}\n</untrusted_content>"
