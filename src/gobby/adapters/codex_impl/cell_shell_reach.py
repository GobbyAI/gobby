"""Decide from a functions.exec cell's code, not its text, whether it can reach a shell."""

from __future__ import annotations

import re
from typing import Any

# Strings, template literals, comments and regex literals are inert text. Blanking them
# first keeps a quoted "tools.exec_command(" from counting as code.
# A slash after one of these punctuators opens a regex literal. After anything else it
# divides; a divide stays in the code, so static_code fails closed on it.
_REGEX_PRECEDERS = frozenset("(,=:[!&|?{;>")
_PRIMITIVE_RE = re.compile(
    r"\b(?:exec_command|write_stdin|exec|eval|Function|constructor|import|require|"
    r"globalThis|Reflect|setTimeout|setInterval|setImmediate)\b"
)
_TOOLS_RE = re.compile(r"\btools\b")
_STATIC_TOOL_CALL_RE = re.compile(r"\btools(?:\.[$A-Za-z_][$\w]*)+\s*\(")
# Left in code after blanking, a slash, quote or backslash is lexical syntax this module
# does not read: a regex literal, division, an unmatched quote, or an escape.
_AMBIGUOUS_RE = re.compile(r"[/\\\"'`]")
# An empty poll only reads a running session's output. A poll that types input can run a
# command in that shell, so only a literal object with no chars key, or whose one chars key
# is "", is blanked (or, for may_type_input, a literal write to a shell an attributed
# execution owns).
_INERT_POLL_RE = re.compile(
    r"""\btools\.write_stdin\s*\(\s*(\{(?:[^{}"]|"(?:\\.|[^"\\])*")*\})\s*\)"""
)
# Keys are names or plain quoted names, values are literals: nothing can run or compute a key.
_LITERAL_KEY = r'(?:[A-Za-z_]\w*|\d+|"[^"\\]*")'
_LITERAL_VALUE = r'(?:-?\d+(?:\.\d+)?|true|false|null|"(?:\\.|[^"\\])*")'
_LITERAL_PAIR = rf"{_LITERAL_KEY}\s*:\s*{_LITERAL_VALUE}"
_LITERAL_OBJECT_RE = re.compile(rf"\{{\s*(?:{_LITERAL_PAIR}(?:\s*,\s*{_LITERAL_PAIR})*)?\s*\}}")
_CHARS_WORD_RE = re.compile(r"\bchars\b")
_EMPTY_CHARS_RE = re.compile(r"""(?:^|[{,])\s*"?chars"?\s*:\s*""\s*(?=[,}])""")
# One literal session ID pair; within a literal object no expression can follow the value.
_SESSION_PAIR_RE = re.compile(r"""(?:^|[{,])\s*"?session_id"?\s*:\s*("[^"\\]*"|-?\d+)\s*(?=[,}])""")


def _blank(chars: list[str], start: int, stop: int) -> None:
    for index in range(start, stop):
        if chars[index] != "\n":
            chars[index] = " "


def _string_end(source: str, start: int) -> int | None:
    """Return the offset just past the string or template opening at ``start``."""
    quote = source[start]
    cursor = start + 1
    while cursor < len(source):
        char = source[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == quote:
            return cursor + 1
        if char == "\n" and quote != "`":
            return None
        cursor += 1
    return None


def _regex_end(source: str, start: int) -> int | None:
    """Return the offset just past the regex literal at ``start``, flags included."""
    in_class = False
    cursor = start + 1
    while cursor < len(source):
        char = source[cursor]
        if char == "\n" or (char == "\\" and source[cursor + 1 : cursor + 2] == "\n"):
            return None
        if char == "\\":
            cursor += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            cursor += 1
            while cursor < len(source) and source[cursor].isalpha():
                cursor += 1
            return cursor
        cursor += 1
    return None


def _mask_inert(source: str) -> str | None:
    """Blank comments, strings, templates and regex literals, keeping every offset.

    The scan runs left to right, so a quote inside a regex literal cannot open a string
    early and hide the code after it. Returns None on any construct it cannot read.
    """
    chars = list(source)
    index = 0
    last = ""
    while index < len(source):
        char = source[index]
        pair = source[index : index + 2]
        if pair == "//":
            stop = source.find("\n", index)
            stop = len(source) if stop == -1 else stop
            _blank(chars, index, stop)
            index = stop
            continue
        if pair == "/*":
            close = source.find("*/", index + 2)
            if close == -1:
                return None
            _blank(chars, index, close + 2)
            index = close + 2
            continue
        if char in "'\"`":
            end = _string_end(source, index)
            if end is None or (char == "`" and "${" in source[index:end]):
                return None
            _blank(chars, index, end)
            last = char
            index = end
            continue
        if char == "/" and last in _REGEX_PRECEDERS:
            end = _regex_end(source, index)
            if end is None:
                return None
            _blank(chars, index, end)
            last = ")"
            index = end
            continue
        if not char.isspace():
            last = char
        index += 1
    return "".join(chars)


def static_code(source: str) -> str | None:
    """Return ``source`` with inert text blanked to spaces, keeping every offset.

    Returns None when a construct cannot be read: a division, an unterminated string, a
    template with a substitution, or a regex literal that spans lines. Callers fail closed
    on None.
    """
    masked = _mask_inert(source)
    if masked is None:
        return None
    return None if _AMBIGUOUS_RE.search(masked) else masked


def _blank_inert_poll(match: re.Match[str], owned_session: str | None) -> str:
    """Replace one write_stdin call that cannot type into an unowned shell with a plain call.

    Its object must be literal, and either it has no chars key (the tool reads that as a
    poll), its one chars key is "", or its one session ID is ``owned_session``, a shell an
    attributed execution owns.
    """
    body = match.group(1)
    if _LITERAL_OBJECT_RE.fullmatch(body) is None:
        return match.group(0)
    chars = len(_CHARS_WORD_RE.findall(body))
    if chars == 0 or (chars == 1 and _EMPTY_CHARS_RE.search(body)):
        return "tools.idle_poll()"
    sessions = [value.strip('"') for value in _SESSION_PAIR_RE.findall(body)]
    if owned_session is not None and sessions == [owned_session]:
        return "tools.idle_poll()"
    return match.group(0)


def _may_run(arguments: Any, harmless: frozenset[str], owned_session: str | None) -> bool:
    """Read cell code statically; any primitive outside ``harmless`` fails closed."""
    if not isinstance(arguments, str) or "${" in arguments:
        return True
    arguments = _INERT_POLL_RE.sub(lambda call: _blank_inert_poll(call, owned_session), arguments)
    code = static_code(arguments)
    if code is None or any(word not in harmless for word in _PRIMITIVE_RE.findall(code)):
        return True
    return len(_TOOLS_RE.findall(code)) != len(_STATIC_TOOL_CALL_RE.findall(code))


def may_reach_shell(arguments: Any) -> bool:
    """Return whether a cell could start a shell whose command this hook cannot read.

    Fails closed on anything it cannot read statically: computed or aliased tool access,
    eval-like primitives, template substitutions, ambiguous lexical syntax, and non-text
    arguments.
    """
    return _may_run(arguments, frozenset(), None)


def may_type_input(arguments: Any, owned_session: str | None = None) -> bool:
    """Return whether a cell could type into a shell that no attributed execution owns.

    A static exec_command call starts a new shell, so it types nothing. A literal empty poll
    (chars omitted or ""), or a literal write to ``owned_session``, is attributed. Everything
    else fails closed as in may_reach_shell, so a computed session ID or chars value counts as
    typing.
    """
    return _may_run(arguments, frozenset({"exec_command"}), owned_session)


def _blank_session_write(match: re.Match[str], session_id: str) -> str:
    """Replace the literal write to ``session_id`` with a call that types nothing."""
    body = match.group(1)
    if _LITERAL_OBJECT_RE.fullmatch(body) is None:
        return match.group(0)
    sessions = [value.strip('"') for value in _SESSION_PAIR_RE.findall(body)]
    return "tools.idle_poll()" if sessions == [session_id] else match.group(0)


def sole_typed_session(
    arguments: Any, session_id: str | None, owned_session: str | None = None
) -> str | None:
    """Return ``session_id`` when its literal write is the cell's only code that can type.

    A failed write to that session then typed nothing, so its latch may release. Any other
    typing code, a computed write or a computed chars value included, keeps the session
    unproven and returns None.
    """
    if session_id is None or not isinstance(arguments, str):
        return session_id
    blanked = _INERT_POLL_RE.sub(lambda call: _blank_session_write(call, session_id), arguments)
    return None if may_type_input(blanked, owned_session) else session_id
