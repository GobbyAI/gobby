"""Telegram-safe Markdown rendering and visible-length chunking."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class _Text:
    value: str


@dataclass(frozen=True, slots=True)
class _OpenTag:
    name: str
    opening: str
    closing: str


@dataclass(frozen=True, slots=True)
class _CloseTag:
    name: str


type _Token = _Text | _OpenTag | _CloseTag

_SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto", "tg"})


def markdown_to_telegram_html_chunks(content: str, max_length: int) -> list[str]:
    """Render supported Markdown to safe Telegram HTML and split by visible length."""
    if max_length < 1:
        raise ValueError("max_length must be positive")
    if not content:
        return [""]
    return _chunk_tokens(_parse_inline(content), max_length)


def _parse_inline(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    plain: list[str] = []
    index = 0

    def flush_plain() -> None:
        if plain:
            tokens.append(_Text("".join(plain)))
            plain.clear()

    while index < len(source):
        if source[index] == "\\" and index + 1 < len(source):
            plain.append(source[index + 1])
            index += 2
            continue

        if source.startswith("```", index):
            end = _find_unescaped(source, "```", index + 3)
            if end is not None:
                flush_plain()
                code = source[index + 3 : end]
                first_line, separator, remainder = code.partition("\n")
                if separator and first_line.strip() and " " not in first_line.strip():
                    code = remainder
                tokens.extend(
                    [
                        _OpenTag("pre", "<pre>", "</pre>"),
                        _OpenTag("code", "<code>", "</code>"),
                        _Text(code),
                        _CloseTag("code"),
                        _CloseTag("pre"),
                    ]
                )
                index = end + 3
                continue

        if source[index] == "`":
            end = _find_unescaped(source, "`", index + 1)
            if end is not None and end > index + 1:
                flush_plain()
                tokens.extend(
                    [
                        _OpenTag("code", "<code>", "</code>"),
                        _Text(source[index + 1 : end]),
                        _CloseTag("code"),
                    ]
                )
                index = end + 1
                continue

        if source[index] == "[":
            label_end = _find_unescaped(source, "](", index + 1)
            if label_end is not None:
                url_end = _find_unescaped(source, ")", label_end + 2)
                if url_end is not None:
                    href = _safe_href(source[label_end + 2 : url_end])
                    if href is not None:
                        flush_plain()
                        tokens.append(
                            _OpenTag(
                                "a",
                                f'<a href="{escape(href, quote=True)}">',
                                "</a>",
                            )
                        )
                        tokens.extend(_parse_inline(source[index + 1 : label_end]))
                        tokens.append(_CloseTag("a"))
                        index = url_end + 1
                        continue

        matched = False
        for delimiter, tag in (("**", "b"), ("__", "b"), ("*", "i"), ("_", "i")):
            if not source.startswith(delimiter, index):
                continue
            if not _can_open_delimiter(source, index, delimiter):
                continue
            end = _find_unescaped(source, delimiter, index + len(delimiter))
            if end is None or not _can_close_delimiter(source, end):
                continue
            flush_plain()
            tokens.append(_OpenTag(tag, f"<{tag}>", f"</{tag}>"))
            tokens.extend(_parse_inline(source[index + len(delimiter) : end]))
            tokens.append(_CloseTag(tag))
            index = end + len(delimiter)
            matched = True
            break
        if matched:
            continue

        plain.append(source[index])
        index += 1

    flush_plain()
    return tokens


def _find_unescaped(source: str, needle: str, start: int) -> int | None:
    position = source.find(needle, start)
    while position >= 0:
        backslashes = 0
        cursor = position - 1
        while cursor >= 0 and source[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            return position
        position = source.find(needle, position + len(needle))
    return None


def _can_open_delimiter(source: str, index: int, delimiter: str) -> bool:
    content_index = index + len(delimiter)
    if content_index >= len(source) or source[content_index].isspace():
        return False
    if delimiter == "_" and index > 0 and source[index - 1].isalnum():
        return False
    return True


def _can_close_delimiter(source: str, index: int) -> bool:
    return index > 0 and not source[index - 1].isspace()


def _safe_href(value: str) -> str | None:
    href = value.strip()
    if not href or any(character.isspace() or ord(character) < 32 for character in href):
        return None
    parsed = urlsplit(href)
    if parsed.scheme.lower() not in _SAFE_LINK_SCHEMES:
        return None
    return href


def _chunk_tokens(tokens: list[_Token], max_length: int) -> list[str]:
    chunks: list[str] = []
    parts: list[str] = []
    active: list[_OpenTag] = []
    visible_length = 0

    def finish_chunk() -> None:
        nonlocal parts, visible_length
        parts.extend(tag.closing for tag in reversed(active))
        chunks.append("".join(parts))
        parts = [tag.opening for tag in active]
        visible_length = 0

    for token in tokens:
        if isinstance(token, _OpenTag):
            parts.append(token.opening)
            active.append(token)
            continue
        if isinstance(token, _CloseTag):
            if not active or active[-1].name != token.name:
                raise ValueError(f"Unbalanced Telegram HTML tag: {token.name}")
            parts.append(active.pop().closing)
            continue

        remaining = token.value
        while remaining:
            available = max_length - visible_length
            if available == 0:
                finish_chunk()
                available = max_length
            if len(remaining) <= available:
                parts.append(escape(remaining, quote=False))
                visible_length += len(remaining)
                break

            split_at = _preferred_split(remaining, available, active)
            piece = remaining[:split_at]
            remainder = remaining[split_at:]
            if not any(tag.name in {"code", "pre"} for tag in active):
                piece = piece.rstrip()
                remainder = remainder.lstrip()
            if not piece:
                piece = remaining[:available]
                remainder = remaining[available:]
            parts.append(escape(piece, quote=False))
            visible_length += len(piece)
            remaining = remainder
            finish_chunk()

    if parts or not chunks:
        parts.extend(tag.closing for tag in reversed(active))
        chunks.append("".join(parts))
    return chunks


def _preferred_split(remaining: str, available: int, active: list[_OpenTag]) -> int:
    if any(tag.name in {"code", "pre"} for tag in active):
        return available
    for index in range(available, 0, -1):
        if remaining[index - 1].isspace():
            return index - 1
    return available
