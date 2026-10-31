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


def static_code(source: str) -> str:
    """Return ``source`` with inert text blanked to spaces, keeping every offset."""
    return _INERT_RE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), source)


def may_reach_shell(arguments: Any) -> bool:
    """Return whether a cell could start a shell whose command this hook cannot read.

    Fails closed on anything it cannot read statically: computed or aliased tool access,
    eval-like primitives, template substitutions, and non-text arguments.
    """
    if not isinstance(arguments, str) or "${" in arguments:
        return True
    code = static_code(arguments)
    if _PRIMITIVE_RE.search(code):
        return True
    return len(_TOOLS_RE.findall(code)) != len(_STATIC_TOOL_CALL_RE.findall(code))
