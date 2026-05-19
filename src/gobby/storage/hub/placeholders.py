"""Shared SQL placeholder remapping for hub database adapters."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def remap_dollar_placeholders(
    sql: str,
    params: Sequence[Any],
    replacement: str,
) -> tuple[str, tuple[Any, ...], tuple[int, ...]]:
    """Rewrite top-level ``$N`` placeholders and return remapped params.

    SQL strings, quoted identifiers, comments, and Postgres dollar-quoted bodies
    are copied unchanged. Only top-level bind placeholders are replaced.
    """
    new_sql, indexes = scan_dollar_placeholder_indexes(sql, len(params), replacement)
    return new_sql, params_from_indexes(params, indexes), indexes


def scan_dollar_placeholder_indexes(
    sql: str,
    param_count: int,
    replacement: str,
) -> tuple[str, tuple[int, ...]]:
    out: list[str] = []
    indexes: list[int] = []
    i = 0
    n = len(sql)

    while i < n:
        char = sql[i]

        if char == "-" and i + 1 < n and sql[i + 1] == "-":
            end = sql.find("\n", i)
            end = n if end < 0 else end
            out.append(sql[i:end])
            i = end
            continue

        if char == "/" and i + 1 < n and sql[i + 1] == "*":
            i = _copy_block_comment(sql, i, out)
            continue

        if char == "'":
            i = _copy_single_quoted_string(sql, i, out)
            continue

        if char == '"':
            i = _copy_double_quoted_identifier(sql, i, out)
            continue

        if char == "$":
            remapped = _try_remap_dollar_token(
                sql,
                i,
                param_count,
                replacement,
                out,
                indexes,
            )
            if remapped is not None:
                i = remapped
                continue

        out.append(char)
        i += 1

    return "".join(out), tuple(indexes)


def params_from_indexes(params: Sequence[Any], indexes: Sequence[int]) -> tuple[Any, ...]:
    remapped: list[Any] = []
    for index in indexes:
        if index >= len(params):
            raise ValueError(
                f"placeholder ${index + 1} has no matching param "
                f"(query references {len(params)} params total)"
            )
        remapped.append(params[index])
    return tuple(remapped)


def _copy_block_comment(sql: str, start: int, out: list[str]) -> int:
    i = start + 2
    depth = 1
    n = len(sql)
    while i < n and depth:
        if i + 1 < n and sql[i] == "/" and sql[i + 1] == "*":
            depth += 1
            i += 2
            continue
        if i + 1 < n and sql[i] == "*" and sql[i + 1] == "/":
            depth -= 1
            i += 2
            continue
        i += 1
    out.append(sql[start:i])
    return i


def _copy_single_quoted_string(sql: str, start: int, out: list[str]) -> int:
    i = start
    n = len(sql)
    out.append(sql[i])
    i += 1

    while i < n:
        if sql[i] == "'":
            if i + 1 < n and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            out.append("'")
            return i + 1
        out.append(sql[i])
        i += 1

    return i


def _copy_double_quoted_identifier(sql: str, start: int, out: list[str]) -> int:
    i = start
    n = len(sql)
    out.append(sql[i])
    i += 1

    while i < n:
        if sql[i] == '"':
            if i + 1 < n and sql[i + 1] == '"':
                out.append('""')
                i += 2
                continue
            out.append('"')
            return i + 1
        out.append(sql[i])
        i += 1

    return i


def _try_remap_dollar_token(
    sql: str,
    start: int,
    param_count: int,
    replacement: str,
    out: list[str],
    indexes: list[int],
) -> int | None:
    if start > 0 and _is_identifier_continuation(sql[start - 1]):
        return None

    tag_end = start + 1
    n = len(sql)
    while tag_end < n and _is_identifier_continuation(sql[tag_end]):
        tag_end += 1

    if tag_end < n and sql[tag_end] == "$":
        tag = sql[start : tag_end + 1]
        close = sql.find(tag, tag_end + 1)
        if close < 0:
            raise ValueError(f"unterminated dollar-quote tag {tag!r}")
        end = close + len(tag)
        out.append(sql[start:end])
        return end

    digits = sql[start + 1 : tag_end]
    if digits and digits.isdigit():
        index = int(digits)
        if index < 1 or index > param_count:
            raise ValueError(
                f"placeholder ${index} has no matching param "
                f"(query references {param_count} params total)"
            )
        out.append(replacement)
        indexes.append(index - 1)
        return tag_end

    return None


def _is_identifier_continuation(char: str) -> bool:
    return char.isalnum() or char == "_"
