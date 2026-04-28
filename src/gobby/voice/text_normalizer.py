"""Text normalization for spoken TTS output."""

from __future__ import annotations

import re

_QUOTE_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
    }
)

_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_FENCE_MARKER_RE = re.compile(r"(?m)^\s*```[A-Za-z0-9_-]*\s*$")
_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_BLOCKQUOTE_RE = re.compile(r"(?m)^\s{0,3}>\s?")
_UNORDERED_LIST_RE = re.compile(r"(?m)^\s*[-+*]\s+")
_ORDERED_LIST_RE = re.compile(r"(?m)^\s*\d+[.)]\s+")
_TASK_MARKER_RE = re.compile(r"(?i)\[[ x]\]\s*")
_HASH_NUMBER_RE = re.compile(r"#(?=\d)")
_MARKDOWN_MARKER_RE = re.compile(r"[*_`~]+")
_BOUNDARY_APOSTROPHE_RE = re.compile(r"(?<![A-Za-z0-9])'|'(?![A-Za-z0-9])")
_DOUBLE_QUOTE_RE = re.compile(r'"')
_BRACKET_RE = re.compile(r"[\[\]{}<>]")
_WORD_JOINER_RE = re.compile(r"[\\/|]")
_SYMBOL_SPACER_RE = re.compile(r"[@$^=]+")
_SPACE_RE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,!?;:])")


def normalize_tts_text(text: str) -> str:
    """Return a spoken form of assistant text without Markdown control symbols."""
    spoken = text.translate(_QUOTE_TRANSLATION)
    spoken = _IMAGE_RE.sub(" ", spoken)
    spoken = _LINK_RE.sub(r"\1", spoken)
    spoken = _FENCE_MARKER_RE.sub(" ", spoken)
    spoken = _HEADING_RE.sub("", spoken)
    spoken = _BLOCKQUOTE_RE.sub("", spoken)
    spoken = _UNORDERED_LIST_RE.sub("", spoken)
    spoken = _ORDERED_LIST_RE.sub("", spoken)
    spoken = _TASK_MARKER_RE.sub("", spoken)
    spoken = _HASH_NUMBER_RE.sub("number ", spoken)
    spoken = spoken.replace("&", " and ").replace("%", " percent ")
    spoken = _MARKDOWN_MARKER_RE.sub(" ", spoken)
    spoken = _BOUNDARY_APOSTROPHE_RE.sub("", spoken)
    spoken = _DOUBLE_QUOTE_RE.sub("", spoken)
    spoken = _BRACKET_RE.sub(" ", spoken)
    spoken = _WORD_JOINER_RE.sub(" ", spoken)
    spoken = _SYMBOL_SPACER_RE.sub(" ", spoken)
    spoken = spoken.replace("#", " ")
    spoken = _SPACE_RE.sub(" ", spoken)
    spoken = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", spoken)
    return spoken.strip()
