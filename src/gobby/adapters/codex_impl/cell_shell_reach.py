"""Decide from a functions.exec cell's code, not its text, whether it can reach a shell."""

from __future__ import annotations

import re
from typing import Any

# Strings, template literals and comments are inert text. Matching them first keeps a
# quoted "tools.exec_command(" from counting as code.
_INERT_RE = re.compile(
    r"""//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`""",
    re.DOTALL,
)
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


def static_code(source: str) -> str | None:
    """Return ``source`` with inert text blanked to spaces, keeping every offset.

    Returns None when the blanking may have mis-lexed: a regex literal's quote would end
    a string early, hiding the code after it. Callers fail closed on None.
    """
    code = _INERT_RE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), source)
    return None if _AMBIGUOUS_RE.search(code) else code


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
