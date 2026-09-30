"""Positive proofs that a shell variable holds a literal path.

Loop headers and leading bare assignments bind variables; these helpers prove a
binding is literal, stable and never rebound before a write operand uses it.
"""

import re

from gobby.hooks._normalization_shell import (
    _is_env_assignment,
    _looks_path_target,
    _strip_shell_wrappers,
)

_BASH_LOOP_BINDING_UNSTABLE_PARAMETERS = frozenset(
    """RANDOM SRANDOM SECONDS LINENO BASHPID BASH_COMMAND BASH_SUBSHELL BASH_ARGV
    BASH_ARGC BASH_ARGV0 BASH_SOURCE BASH_LINENO BASH_VERSINFO BASH_VERSION
    BASH_ALIASES BASH_CMDS BASH_EXECUTION_STRING BASH_REMATCH FUNCNAME HISTCMD
    EPOCHSECONDS EPOCHREALTIME PIPESTATUS DIRSTACK GROUPS UID EUID PPID SHELLOPTS
    BASHOPTS OPTIND OPTARG OPTERR REPLY COMP_WORDS COMP_CWORD COMP_LINE COMP_POINT
    COMP_KEY COMP_TYPE COMPREPLY IFS PATH HOME PWD OLDPWD SHLVL _""".split()
)
_SHELL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*$")


def _loop_binding_variable_is_stable(variable: str) -> bool:
    """Return whether Bash gives ``variable`` ordinary scalar expansion semantics."""
    return bool(
        _SHELL_IDENTIFIER_RE.fullmatch(variable)
        and variable not in _BASH_LOOP_BINDING_UNSTABLE_PARAMETERS
    )


def _raw_shell_word_is_literal(raw_word: str) -> bool:
    """Prove a source word contains no active Bash expansion or glob syntax."""
    in_single_quote = False
    in_double_quote = False
    escaped = False
    for index, char in enumerate(raw_word):
        if escaped:
            escaped = False
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            continue
        if in_single_quote:
            continue
        if char in "$`":
            return False
        if not in_double_quote and (char in "*?[{}" or (index == 0 and char == "~")):
            return False
    return not escaped and not in_single_quote and not in_double_quote


def _loop_header_words_are_literal(
    words: tuple[str, ...],
    raw_words: tuple[str, ...],
) -> bool:
    """Prove every word in a ``for <var> in ...`` header is a literal path."""
    if not words or not raw_words or len(words) != len(raw_words):
        return not words and not raw_words
    try:
        for_index = words.index("for")
    except ValueError:
        return False
    if len(words) <= for_index + 3 or words[for_index + 2] != "in":
        return False
    return all(
        _looks_path_target(value) and _raw_shell_word_is_literal(raw)
        for value, raw in zip(words[for_index + 3 :], raw_words[for_index + 3 :], strict=True)
    )


def _shell_loop_binding_disqualifications(words: tuple[str, ...]) -> frozenset[str]:
    """Return variables whose command-local attributes make loop binding unsafe."""
    disqualified: set[str] = set()
    cursor = 0
    if words[:1] == ("env",):
        cursor = 1
    while cursor < len(words) and _is_env_assignment(words[cursor]):
        disqualified.add(words[cursor].partition("=")[0])
        cursor += 1

    parts = _strip_shell_wrappers(list(words))
    if not parts:
        # A bare assignment rebinds the shell variable itself, which the binding
        # pass replaces or drops; only command-scoped prefixes disqualify.
        return frozenset(disqualified) if words[:1] == ("env",) else frozenset()
    command = parts[0].rsplit("/", 1)[-1]
    declares_attributes = command in {"readonly", "export"}
    if command in {"declare", "local", "typeset"}:
        option_letters = "".join(part[1:] for part in parts[1:] if part.startswith("-"))
        declares_attributes = any(flag in option_letters for flag in "inr")
    if declares_attributes:
        for part in parts[1:]:
            if part.startswith(("-", "+")):
                continue
            name = part.partition("=")[0].partition("[")[0]
            if _SHELL_IDENTIFIER_RE.fullmatch(name):
                disqualified.add(name)
    return frozenset(disqualified)


def _literal_assignment_bindings(
    words: tuple[str, ...],
    raw_words: tuple[str, ...],
) -> tuple[tuple[str, str], ...] | None:
    """Return absolute literal bindings of a bare-assignment segment, else ``None``."""
    if not words or len(words) != len(raw_words):
        return None
    bindings: dict[str, str] = {}
    for word, raw in zip(words, raw_words, strict=True):
        name, _, value = word.partition("=")
        if not _is_env_assignment(word) or not raw.startswith(f"{name}="):
            return None
        raw_value = raw[len(name) + 1 :]
        if value.startswith("/") and "~" not in raw_value and _raw_shell_word_is_literal(raw_value):
            bindings[name] = value
        else:
            bindings.pop(name, None)
    return tuple(bindings.items())


def _plain_loop_binding_reference(
    path: str,
    words: tuple[str, ...],
    raw_words: tuple[str, ...],
) -> tuple[str, str] | None:
    """Return ``(variable, literal suffix)`` for one exact double-quoted expansion."""
    match = re.fullmatch(
        r"\$(?:\{(?P<braced>[A-Za-z_]\w*)\}|(?P<bare>[A-Za-z_]\w*))(?P<suffix>[^$`\\\"]*)", path
    )
    if not match or not raw_words:
        return None
    variable = match.group("braced") or match.group("bare")
    suffix = match.group("suffix")
    raw_matches = [raw for word, raw in zip(words, raw_words, strict=True) if word == path]
    allowed = {f'"${variable}{suffix}"', f'"${{{variable}}}{suffix}"'}
    if raw_matches and all(raw in allowed for raw in raw_matches):
        return variable, suffix
    return None


_LOOP_BINDING_UNSAFE_COMMANDS = frozenset(
    {".", "declare", "eval", "getopts", "mapfile", "read", "readarray", "source", "unset"}
)


def _shell_segment_preserves_loop_binding(
    words: tuple[str, ...],
    variable: str,
    *,
    command_is_known: bool,
) -> bool:
    """Prove that one simple shell segment cannot rebind ``variable``."""
    assignment = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(variable)}(?:\[[^]]*\])?\+?=")
    if any("$(" in word or "`" in word or assignment.search(word) for word in words):
        return False

    parts = _strip_shell_wrappers(list(words))
    if not parts:
        return True
    command = parts[0].rsplit("/", 1)[-1]
    if command in _LOOP_BINDING_UNSAFE_COMMANDS:
        return False
    if command == "printf" and "-v" in parts[1:]:
        return False
    return command_is_known
