"""Delimited source scanning helpers for test-quality analysis."""

from __future__ import annotations


def _find_matching_delimiter(
    source: str, open_index: int, open_char: str, close_char: str
) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    index = open_index
    rust_body = open_char == "{" and close_char == "}"

    while index < len(source):
        char = source[index]

        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue

        if rust_body:
            raw_string_end = _rust_raw_string_end(source, index)
            if raw_string_end is not None:
                index = raw_string_end + 1
                continue

            if char == "'":
                char_literal_end = _rust_char_literal_end(source, index)
                if char_literal_end is not None:
                    index = char_literal_end + 1
                    continue

                lifetime_end = _rust_lifetime_end(source, index)
                if lifetime_end is not None:
                    index = lifetime_end
                    continue

        if char in {"'", '"', "`"}:
            quote = char
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index

        index += 1

    return None


def _rust_raw_string_end(source: str, index: int) -> int | None:
    if source[index] != "r":
        return None

    cursor = index + 1
    while cursor < len(source) and source[cursor] == "#":
        cursor += 1

    if cursor >= len(source) or source[cursor] != '"':
        return None

    hashes = cursor - index - 1
    terminator = '"' + ("#" * hashes)
    end = source.find(terminator, cursor + 1)
    if end == -1:
        return None
    return end + len(terminator) - 1


def _rust_char_literal_end(source: str, index: int) -> int | None:
    cursor = index + 1
    if cursor >= len(source):
        return None

    if source[cursor] == "\\":
        cursor += 1
        if cursor >= len(source):
            return None
        if source[cursor] == "u" and cursor + 1 < len(source) and source[cursor + 1] == "{":
            cursor = source.find("}", cursor + 2)
            if cursor == -1:
                return None
        cursor += 1
    else:
        if source[cursor] in {"\n", "\r", "'"}:
            return None
        cursor += 1

    if cursor < len(source) and source[cursor] == "'":
        return cursor
    return None


def _rust_lifetime_end(source: str, index: int) -> int | None:
    cursor = index + 1
    if cursor >= len(source) or not (source[cursor].isalpha() or source[cursor] == "_"):
        return None
    cursor += 1
    while cursor < len(source) and (source[cursor].isalnum() or source[cursor] == "_"):
        cursor += 1
    return cursor


def _next_non_whitespace_index(source: str, start: int) -> int | None:
    for index in range(start, len(source)):
        if not source[index].isspace():
            return index
    return None
