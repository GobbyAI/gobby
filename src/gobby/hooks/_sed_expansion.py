"""Expand a sed program word whose expansions are proven before sed parses it.

Only two expansions qualify, both inside double quotes so no splitting or
globbing follows: ``$((...))`` arithmetic, whose result is an integer, and
``$name``/``${name}`` bound to a literal by the command's leading bare
assignments. Anything else leaves the program unresolved.
"""

import re
from collections.abc import Mapping
from contextvars import ContextVar
from types import MappingProxyType

# Literal values of the leading bare assignments, visible to the command they precede.
sed_literal_bindings: ContextVar[Mapping[str, str]] = ContextVar(
    "sed_literal_bindings", default=MappingProxyType({})
)
_PARAMETER = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")
# Names, numbers and operators only: no subscript, substitution or quote reaches eval.
_ARITHMETIC_BODY = re.compile(r"(?:[\w\s+\-*/%()]|\$[A-Za-z_])*")
# Stands in for an arithmetic result when the program is checked for write commands.
_ARITHMETIC_RESULT = "1"


def expand_sed_program(raw_word: str, bindings: Mapping[str, str]) -> tuple[str, bool] | None:
    """Return the program sed receives and whether arithmetic stood in for a value.

    None means some part of the word is not proven literal.
    """
    expanded: list[str] = []
    arithmetic = False
    quoted = False
    index = 0
    while index < len(raw_word):
        char = raw_word[index]
        if char == "'" and not quoted:
            end = raw_word.find("'", index + 1)
            if end < 0:
                return None
            expanded.append(raw_word[index + 1 : end])
            index = end + 1
        elif char == '"':
            quoted = not quoted
            index += 1
        elif char == "\\":
            if index + 1 >= len(raw_word):
                return None
            escaped = raw_word[index + 1]
            if quoted and escaped not in '$`"\\\n':
                expanded.append(char)
            expanded.append(escaped)
            index += 2
        elif char == "$":
            if not quoted:
                return None
            if raw_word.startswith("$((", index):
                close = _arithmetic_end(raw_word, index + 3)
                if close is None or not _ARITHMETIC_BODY.fullmatch(raw_word, index + 3, close):
                    return None
                expanded.append(_ARITHMETIC_RESULT)
                arithmetic = True
                index = close + 2
                continue
            match = _PARAMETER.match(raw_word, index)
            if match is None or (name := match.group(1) or match.group(2)) not in bindings:
                return None
            expanded.append(bindings[name])
            index = match.end()
        elif char == "`" or (not quoted and (char.isspace() or char in "*?[]{}~")):
            return None
        else:
            expanded.append(char)
            index += 1
    return None if quoted else ("".join(expanded), arithmetic)


def _arithmetic_end(word: str, start: int) -> int | None:
    """Index of the ``))`` closing an arithmetic expansion whose body begins at ``start``."""
    depth = 0
    for index in range(start, len(word)):
        if word[index] == "(":
            depth += 1
        elif word[index] == ")":
            if not depth:
                return index if word.startswith("))", index) else None
            depth -= 1
    return None
