"""Static shell argument provenance, independent of execution receipt correlation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from gobby.adapters.codex_impl.cell_shell_reach import may_reach_shell, static_code

_CALL = re.compile(r"\btools\.exec_command\s*\(")
_REPEATED = re.compile(r"\b(?:do|for|while|function)\b|=>|\.(?:forEach|map|reduce)\s*\(")
_KEY = re.compile(r'("(?:\\.|[^"\\])*"|[$A-Za-z_][$\w]*)\s*:')
_ELSE_RE = re.compile(r"else(?![\w$])")


def literal_shell_calls(arguments: Any) -> list[dict[str, Any]] | None:
    """Prove every literal shell in a cell; a batch member needs an explicit absolute cwd."""
    calls = literal_exec_calls(arguments)
    if calls is not None and len(calls) > 1:
        for call in calls:
            workdir = call.get("workdir")
            if not isinstance(workdir, str) or not Path(workdir).is_absolute():
                return None
    return calls


def literal_exec_calls(arguments: Any) -> list[dict[str, Any]] | None:
    """Enumerate every statically invoked literal shell, without requiring any cwd.

    Attribution reads a batch's members from this list; only ``literal_shell_calls`` proves
    their cwd. Any shell that cannot be read (dynamic, looped, repeated, unparsed) is None.
    """
    if not isinstance(arguments, str):
        return None
    # Known direct shells are inspected below; reject every other dynamic shell path.
    if may_reach_shell(_CALL.sub("tools.literal_shell(", arguments)):
        return None
    code = static_code(arguments)
    if code is None:
        return None
    spans = repeated_spans(code)
    if spans is None:
        return None
    matches = list(_CALL.finditer(code))
    calls = []
    for match in matches:
        if any(start <= match.start() < end for start, end in spans):
            return None
        fields = _literal_arguments(arguments[match.end() :].lstrip())
        if fields is None:
            return None
        command = fields.get("cmd")
        if not isinstance(command, str) or not command:
            return None
        workdir = fields.get("workdir")
        if "workdir" in fields and (not isinstance(workdir, str) or not workdir):
            return None
        calls.append(fields)
    return calls or None


def _literal_arguments(source: str) -> dict[str, Any] | None:
    """Decode a JS object with JSON literal values and no overwritten or computed fields."""
    if not source.startswith("{"):
        return None
    decoder = json.JSONDecoder()
    fields: dict[str, Any] = {}
    source = source[1:].lstrip()
    while source and not source.startswith("}"):
        key_match = _KEY.match(source)
        if key_match is None:
            return None
        raw_key = key_match[1]
        try:
            key = json.loads(raw_key) if raw_key.startswith('"') else raw_key
        except ValueError:
            return None
        if key in fields:
            return None
        source = source[key_match.end() :].lstrip()
        try:
            value, end = decoder.raw_decode(source)
        except ValueError:
            return None
        fields[key] = value
        source = source[end:].lstrip()
        if source.startswith(","):
            source = source[1:].lstrip()
        elif not source.startswith("}"):
            return None
    return fields if source.startswith("}") and source[1:].lstrip().startswith(")") else None


_PAIRS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = frozenset(_PAIRS.values())


def repeated_spans(code: str) -> list[tuple[int, int]] | None:
    """Return spans that may run zero or many times: loop, callback and function bodies.

    None means a span could not be bounded, so the caller fails closed.
    """
    spans = []
    for match in _REPEATED.finditer(code):
        end = _scope_end(code, match)
        if end is None:
            return None
        spans.append((match.start(), end))
    return spans


def _scope_end(code: str, match: re.Match[str]) -> int | None:
    keyword = match.group(0)
    if keyword in ("=>", "do"):
        return _body_end(code, match.end())
    if keyword == "function":
        return _function_end(code, match.end())
    if keyword.startswith("."):
        return _close(code, match.end() - 1)
    # for and while: an optional await, a parenthesized header, then the body.
    header = _skip_space(code, match.end())
    if code.startswith("await", header):
        header = _skip_space(code, header + len("await"))
    if code[header : header + 1] != "(":
        return None
    header_end = _close(code, header)
    return None if header_end is None else _body_end(code, header_end)


def _function_end(code: str, start: int) -> int | None:
    params = code.find("(", start)
    params_end = None if params < 0 else _close(code, params)
    if params_end is None:
        return None
    brace = _skip_space(code, params_end)
    if code[brace : brace + 1] != "{":
        return None
    return _close(code, brace)


def _body_end(code: str, start: int) -> int | None:
    """Return the end of a loop or arrow body: a braced block, or one statement."""
    index = _skip_space(code, start)
    if code[index : index + 1] == "{":
        return _close(code, index)
    return _statement_end(code, index)


def _skip_space(code: str, index: int) -> int:
    while index < len(code) and code[index].isspace():
        index += 1
    return index


def _close(code: str, start: int) -> int | None:
    """Return the index just past the bracket opened at ``start``, or None if unbalanced."""
    expected: list[str] = []
    for index in range(start, len(code)):
        char = code[index]
        if char in _PAIRS:
            expected.append(_PAIRS[char])
        elif char in _CLOSERS:
            if not expected or expected.pop() != char:
                return None
            if not expected:
                return index + 1
    return None


def _statement_end(code: str, start: int) -> int | None:
    """Return where an unbraced body ends: the `;` that no `else` follows, or the closer after it.

    A comma does not end the body: `a, b` is one statement, so a launch after the comma repeats.
    """
    expected: list[str] = []
    for index in range(start, len(code)):
        char = code[index]
        if char in _PAIRS:
            expected.append(_PAIRS[char])
        elif char in _CLOSERS:
            if not expected:
                return index
            if expected.pop() != char:
                return None
        elif char == ";" and not expected:
            if not _ELSE_RE.match(code, _skip_space(code, index + 1)):
                return index + 1
    return len(code)
