"""Expand the variable command words a command line sets before they are matched.

Command matching reads words as text, but the shell expands ``$name`` before it runs a
word: ``x=ps; $x aux`` runs ``ps aux``. This module tracks the literal assignments of one
command line and expands references to them, so a decoded word matches the rules a
literal ``ps`` does.

The pinned ps shim is known without an assignment: ``gobby.agents.constants.GOBBY_PS_SHIM``
names a ``ps`` in every seat that sets it. A name the command line never sets stays text,
so a value that comes from the environment is outside this check. A parameter operator
other than a default (``${x:0:2}``, ``${!x}``) cannot be evaluated here, so it reads as
``ps``, the reading that must not run unseen.
"""

from __future__ import annotations

import re
import shlex

from gobby.agents.constants import GOBBY_PS_SHIM

# The ps reading given to an operator this matcher cannot evaluate.
_OPAQUE_VALUE = "ps"
_ASSIGNMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)
# One level of nested braces is enough for ``${x:-${y}}``; deeper nesting reads as opaque.
_REFERENCE = re.compile(
    r"\$(?:\{(?P<indirect>!?)(?P<braced>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<operator>(?:[^{}]|\$\{[^{}]*\})*)\}"
    r"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)
_DEFAULT = re.compile(r":?-(.*)", re.DOTALL)
# Builtins whose leading assignments set shell variables, as plain assignments do.
_DECLARATIONS = frozenset({"export", "declare", "local", "typeset", "readonly"})


def initial_names() -> dict[str, str]:
    """Return the names a command line resolves before it assigns any."""
    return {GOBBY_PS_SHIM: "ps"}


def expand_stage(words: list[str], names: dict[str, str]) -> list[str]:
    """Expand one pipeline stage's words, then record the assignments it makes.

    A leading assignment, or one after ``export`` and its kin, sets ``names`` for the
    stages that follow. Its value is not split. Other words split on whitespace the way
    an unquoted expansion does, and a word that expands to nothing drops out.
    """
    expanded: list[str] = []
    assigned: dict[str, str] = {}
    leading = True
    for index, word in enumerate(words):
        assignment = _ASSIGNMENT.fullmatch(word)
        if leading and assignment:
            value = _expand(assignment[2], names)
            assigned[assignment[1]] = value
            expanded.append(f"{assignment[1]}={value}")
            continue
        leading = index == 0 and word in _DECLARATIONS
        value = _expand(word, names)
        expanded.extend([word] if value == word else value.split())
    names.update(assigned)
    return expanded


def assignment_prefix(names: dict[str, str]) -> str:
    """Return shell text that restores the literal names a command line assigned.

    A nested script (``bash -c``, ``eval``, a piped shell) is parsed on its own, so the
    names it may read are passed in as assignments ahead of it. The pinned shim is not
    passed: every nested parse starts with it already known.
    """
    assigned = {
        name: value for name, value in names.items() if (name, value) != (GOBBY_PS_SHIM, "ps")
    }
    return "".join(f"{name}={shlex.quote(value)}; " for name, value in assigned.items())


def _expand(text: str, names: dict[str, str]) -> str:
    return _REFERENCE.sub(lambda match: _reference_value(match, names), text)


def _reference_value(match: re.Match[str], names: dict[str, str]) -> str:
    if match["bare"] is not None:
        return names.get(match["bare"], match[0])
    name, operator = match["braced"], match["operator"]
    default = _DEFAULT.fullmatch(operator)
    if match["indirect"] or (operator and ("$" in operator or default is None)):
        return _OPAQUE_VALUE
    if name in names and not (default and operator.startswith(":") and not names[name]):
        return names[name]
    return _expand(default[1], names) if default else match[0]
