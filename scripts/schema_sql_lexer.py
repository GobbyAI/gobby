"""SQL token scanners shared by schema comparison and flattening."""

from collections.abc import Iterator


def _split_sql_statements(source: str) -> Iterator[str]:
    """Split SQL statements while preserving strings, comments, and dollar bodies."""
    statement_start = 0
    index = 0
    while index < len(source):
        char = source[index]
        if char == "-" and source.startswith("--", index):
            index = _skip_line_comment(source, index)
            continue
        if char == "/" and source.startswith("/*", index):
            index = _skip_block_comment(source, index)
            continue
        if char == "'":
            prefix = source[index - 1] if index > 0 else ""
            escape_backslashes = (
                prefix in "eE"
                and bool(prefix)
                and (index < 2 or not _is_identifier_continuation(source[index - 2]))
            )
            index = _skip_single_quoted_string(
                source,
                index,
                escape_backslashes=escape_backslashes,
            )
            continue
        if char == '"':
            index = _skip_double_quoted_identifier(source, index)
            continue
        if char == "$":
            tag = _dollar_quote_tag_at(source, index)
            if tag is not None:
                close = source.find(tag, index + len(tag))
                if close < 0:
                    raise ValueError(f"unterminated dollar-quote tag {tag!r}")
                index = close + len(tag)
                continue
        if char == ";":
            yield source[statement_start:index]
            statement_start = index + 1
        index += 1
    if tail := source[statement_start:]:
        yield tail


def _skip_line_comment(source: str, start: int) -> int:
    end = source.find("\n", start + 2)
    return len(source) if end < 0 else end + 1


def _skip_block_comment(source: str, start: int) -> int:
    index = start + 2
    depth = 1
    while index < len(source) and depth:
        if source.startswith("/*", index):
            depth += 1
            index += 2
            continue
        if source.startswith("*/", index):
            depth -= 1
            index += 2
            continue
        index += 1
    return index


def _skip_single_quoted_string(
    source: str,
    start: int,
    *,
    escape_backslashes: bool,
) -> int:
    index = start + 1
    while index < len(source):
        if escape_backslashes and source[index] == "\\":
            index += 2
            continue
        if source[index] == "'":
            if index + 1 < len(source) and source[index + 1] == "'":
                index += 2
                continue
            return index + 1
        index += 1
    return index


def _skip_double_quoted_identifier(source: str, start: int) -> int:
    index = start + 1
    while index < len(source):
        if source[index] == '"':
            if index + 1 < len(source) and source[index + 1] == '"':
                index += 2
                continue
            return index + 1
        index += 1
    return index


def _dollar_quote_tag_at(source: str, start: int) -> str | None:
    if start > 0 and _is_identifier_continuation(source[start - 1]):
        return None
    if start + 1 >= len(source):
        return None
    if source[start + 1] == "$":
        return "$$"
    if not _is_identifier_start(source[start + 1]):
        return None
    tag_end = start + 2
    while tag_end < len(source) and _is_identifier_continuation(source[tag_end]):
        tag_end += 1
    if tag_end < len(source) and source[tag_end] == "$":
        return source[start : tag_end + 1]
    return None


def _is_identifier_start(char: str) -> bool:
    return char.isalpha() or char == "_"


def _is_identifier_continuation(char: str) -> bool:
    return char.isalnum() or char == "_"


def _split_top_level_commas(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            elif char == "\\" and quote == "'":
                index += 2
                continue
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(value[start:index])
            start = index + 1
        index += 1
    parts.append(value[start:])
    return parts
