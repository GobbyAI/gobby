"""Recognize protected brace spellings without generating shell expansions.

Arbitrary argv launchers make command-position inference unsafe. Match the two
protected basenames anywhere, retaining quote provenance and ordinary brace data.
Each alternative carries at most len(target) + 1 offsets, never expanded strings.
"""

from __future__ import annotations

import fnmatch
import re

from gobby.hooks._ansi_c import opens_ansi_c_quote
from gobby.hooks._normalization_compound import (
    command_substitution_bodies,
    process_substitution_bodies,
    shell_function_bodies,
)
from gobby.hooks._normalization_env import _ASSIGNMENT
from gobby.hooks._normalization_shell import (
    ShellScan,
    is_fd_duplication_token,
    is_shell_input_redirection_token,
    is_shell_output_redirection_token,
    scan_shell_command,
)
from gobby.hooks._shell_execution_words import _unwrap
from gobby.hooks.provider_launch_guard import _python_name, _python_program

from .condition_helpers import _GIT_GLOBAL_FLAGS, _GIT_GLOBAL_VALUE_OPTIONS
from .engine.command_matching import (
    _decoded_stages,
    _shell_input_scripts,
    _wrapper_scripts,
    executable_command_subjects,
)
from .engine.command_matching_uv import _uv_run_argvs

_TARGETS = ("gobby", "approve-dirty-paths")
_MAX_DEPTH = 32
_DATA_COMMANDS = frozenset(
    {"echo", "printf", "rg", "grep", "gcode", "cat", "ls", "wc", "git", "cp"}
)
_DATA_BASENAME_HEADS = (_DATA_COMMANDS - {"git"}) | {"cd", "curl", "test", "["}
# Git subcommands whose operands are revisions or pathspecs. Git runs a program
# from a positional only for subcommands such as `bisect run`, so these stay data.
_GIT_READ_ONLY_SUBCOMMANDS = frozenset(
    {
        "blame",
        "cat-file",
        "describe",
        "diff",
        "log",
        "ls-files",
        "ls-tree",
        "rev-list",
        "rev-parse",
        "show",
        "shortlog",
        "status",
    }
)
_ASSIGNMENT_HEADS = frozenset({"export", "declare", "local", "readonly", "typeset"})
# Reserved words that introduce a simple command, so the command word follows them.
_COMPOUND_PREFIXES = frozenset({"!", "{", "do", "else", "elif", "if", "then", "until", "while"})
type _Character = tuple[str, bool]
# Literal input contributes one character at a time, so this marker cannot
# collide with a glob star or any other character from the command text.
_EXPANSION = "<expansion>"


class _AmbiguousSyntax(ValueError):
    """An unsupported expansion cannot establish that a brace word is safe."""


def _expansion_span(raw: str, index: int) -> tuple[int, bool]:
    """Consume a balanced expansion without evaluating its contents.

    Nested spans share one forward cursor. Their contents become one symbolic
    wildcard, while ordinary braces remain a reason to inspect the word.
    """
    backtick = raw[index] == "`"
    contexts = [("`" if backtick else "}" if raw[index + 1] == "{" else ")", "")]
    shell_contexts = int(contexts[0][0] != "}")
    brace_group = False
    quote = ""
    index += 1 if backtick else 2
    while index < len(raw):
        char = raw[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote in {"'", "$'"}:
            if char == "'":
                quote = ""
        elif not quote and opens_ansi_c_quote(raw, index):
            quote = "$'"
            index += 2
            continue
        elif raw.startswith("${", index):
            contexts.append(("}", quote))
            quote = ""
            index += 2
            continue
        elif raw.startswith(("$(", "<(", ">("), index):
            contexts.append((")", quote))
            shell_contexts += 1
            quote = ""
            index += 2
            continue
        elif not quote and char == contexts[-1][0]:
            closer, quote = contexts.pop()
            if closer != "}":
                shell_contexts -= 1
            if not contexts:
                return index + 1, brace_group
        elif char == "`":
            contexts.append(("`", quote))
            shell_contexts += 1
            quote = ""
        elif not quote and char == "{":
            brace_group = brace_group or not shell_contexts
            contexts.append(("}", quote))
        elif not quote and char == "(" and contexts[-1][0] == ")":
            contexts.append((")", quote))
            shell_contexts += 1
        elif char == quote:
            quote = ""
        elif not quote and char in "'\"":
            quote = char
        index += 1
    raise _AmbiguousSyntax("Unclosed expansion span")


def _characters(
    raw: str, *, parameter_wildcard: bool = True, quoted_expansions: bool = True
) -> list[_Character]:
    """Remove shell quoting, preserving which characters can be syntax."""
    result: list[_Character] = []
    quote = ""
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\" and quote != "'" and index + 1 < len(raw):
            following = raw[index + 1]
            if not quote or following in '$`"\\\n':
                if following != "\n":
                    result.append((following, False))
                index += 2
                continue
        if quote != "'" and (raw.startswith(("${", "$("), index) or char == "`"):
            index, _ = _expansion_span(raw, index)
            result.append(
                (_EXPANSION if parameter_wildcard else "$", not quote or quoted_expansions)
            )
            continue
        if (
            quote != "'"
            and char == "$"
            and index + 1 < len(raw)
            and raw[index + 1] in "0123456789@*#?$!-"
        ):
            index += 2
            result.append(
                (_EXPANSION if parameter_wildcard else "$", not quote or quoted_expansions)
            )
            continue
        if (
            quote != "'"
            and char == "$"
            and index + 1 < len(raw)
            and raw[index + 1] in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
        ):
            index += 2
            while index < len(raw) and raw[index] in (
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789"
            ):
                index += 1
            result.append(
                (_EXPANSION if parameter_wildcard else "$", not quote or quoted_expansions)
            )
            continue
        if char == quote:
            quote = ""
        elif not quote and char in "'\"":
            quote = char
        else:
            result.append(
                (char, not quote or (quoted_expansions and quote == '"' and char in "$`"))
            )
        index += 1
    if quote:
        raise _AmbiguousSyntax("Unclosed quote")
    return result


def _groups(chars: list[_Character]) -> dict[int, tuple[int, list[int], bool]]:
    groups: dict[int, tuple[int, list[int], bool]] = {}
    stack: list[tuple[int, list[int]]] = []
    nested: set[int] = set()
    for index, (char, active) in enumerate(chars):
        if not active:
            continue
        if char == "{":
            if stack:
                nested.add(stack[-1][0])
            stack.append((index, []))
        elif char == "," and stack:
            stack[-1][1].append(index)
        elif char == "}" and stack:
            opening, commas = stack.pop()
            groups[opening] = (index, commas, opening in nested)
    return groups


def _range(chars: list[_Character], start: int, end: int) -> tuple[int, int, int] | None:
    if end - start > 64:
        raise _AmbiguousSyntax("Oversized range expression")
    body = "".join(char for char, _ in chars[start:end])
    # zsh recognizes ranges after removing quotes/escapes on their components.
    # The enclosing braces must still be unquoted (checked by _groups).
    if re.fullmatch(r"-?\d+\.\.-?\d+(?:\.\.-?\d+)?", body):
        # Neither protected basename has a digit.
        return (0, 0, 1)
    match = re.fullmatch(r"(.)\.\.(.)(?:\.\.(-?\d+))?", body)
    if not match:
        return None
    first, last, step = match.groups()
    return ord(first), ord(last), abs(int(step or "1")) or 1


def _in_range(value: str, interval: tuple[int, int, int]) -> bool:
    first, last, step = interval
    code = ord(value)
    return min(first, last) <= code <= max(first, last) and abs(code - first) % step == 0


def _in_brace_ccl(value: str, body: list[_Character]) -> bool:
    """zsh BRACE_CCL selects characters, including inclusive x-y spans."""
    if any(char == _EXPANSION and active for char, active in body):
        return True
    index = 0
    while index < len(body):
        first = body[index][0]
        if index + 2 < len(body) and body[index + 1][0] == "-":
            last = body[index + 2][0]
            if min(ord(first), ord(last)) <= ord(value) <= max(ord(first), ord(last)):
                return True
            index += 3
        else:
            if value == first:
                return True
            index += 1
    return False


def _matches(chars: list[_Character], target: str, *, data_operand: bool = False) -> bool:
    groups = _groups(chars)

    def sequence(start: int, end: int, offsets: set[int], depth: int) -> set[int]:
        if depth > _MAX_DEPTH:
            raise _AmbiguousSyntax("Brace nesting limit")
        index = start
        while index < end:
            char, active = chars[index]
            group = groups.get(index) if active and char == "{" else None
            if group:
                close, commas, nested = group
                if commas:
                    choices: set[int] = set()
                    beginning = index + 1
                    for boundary in (*commas, close):
                        choices.update(sequence(beginning, boundary, offsets, depth + 1))
                        beginning = boundary + 1
                    offsets = choices
                    index = close + 1
                    continue
                interval = None if nested else _range(chars, index + 1, close)
                if interval is not None:
                    offsets = {
                        offset + 1
                        for offset in offsets
                        if offset < len(target) and _in_range(target[offset], interval)
                    } | ({0} if _in_range("/", interval) else set())
                    index = close + 1
                    continue
                if close > index + 1:
                    body = chars[index + 1 : close]
                    if nested or any(not active for _, active in body):
                        raise _AmbiguousSyntax("Nested or quoted BRACE_CCL body")
                    offsets = {
                        offset + 1
                        for offset in offsets
                        if offset < len(target) and _in_brace_ccl(target[offset], body)
                    } | ({0} if _in_brace_ccl("/", body) else set())
                    index = close + 1
                    continue
            if char == "/":
                offsets = {0}
            elif active and char == _EXPANSION:
                # An expansion may supply a slash and any basename prefix,
                # including after literal directory text emptied the states.
                offsets = (
                    {end for offset in offsets for end in range(offset, len(target) + 1)}
                    | set(range(len(target)))
                    if data_operand
                    else set(range(len(target) + 1))
                )
            elif active and char == "*":
                offsets = {end for offset in offsets for end in range(offset, len(target) + 1)}
            elif active and char == "?":
                offsets = {offset + 1 for offset in offsets if offset < len(target)}
            elif active and char == "[":
                closing = index + 1
                if closing < end and chars[closing][0] in "!^":
                    closing += 1
                if closing < end and chars[closing] == ("]", True):
                    raise _AmbiguousSyntax("Leading closing bracket in character class")
                while closing < end and chars[closing] != ("]", True):
                    closing += 1
                if closing == end:
                    raise _AmbiguousSyntax("Unclosed character class in brace word")
                if any(not active or value in "{}" for value, active in chars[index + 1 : closing]):
                    raise _AmbiguousSyntax("Quoted or brace syntax in character class")
                pattern = "".join(value for value, _ in chars[index : closing + 1])
                if "[" in pattern[1:]:
                    raise _AmbiguousSyntax("Extended character class in brace word")
                if pattern.startswith("[^"):
                    pattern = "[!" + pattern[2:]
                offsets = {
                    offset + 1
                    for offset in offsets
                    if offset < len(target) and fnmatch.fnmatchcase(target[offset], pattern)
                }
                index = closing
            else:
                offsets = {
                    offset + 1
                    for offset in offsets
                    if offset < len(target) and target[offset] == char
                }
            index += 1
        return offsets

    return len(target) in sequence(0, len(chars), {0}, 0)


def _word_syntax(raw: str) -> tuple[bool, bool]:
    """Find active braces and expansions, preserving quoted literal data."""
    quote = ""
    expansion = False
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if not quote and opens_ansi_c_quote(raw, index):
            quote = "$'"
            index += 2
            continue
        if quote not in {"'", "$'"} and (raw.startswith(("${", "$("), index) or char == "`"):
            try:
                index, brace_group = _expansion_span(raw, index)
            except _AmbiguousSyntax:
                return True, True
            if brace_group:
                return True, True
            expansion = True
            continue
        if quote not in {"'", "$'"} and char == "$" and index + 1 < len(raw):
            if raw[index + 1] in (
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789@*#?$!-"
            ):
                expansion = True
        if quote == "$'" and char == "'":
            quote = ""
        elif char == quote:
            quote = ""
        elif not quote and char in "'\"":
            quote = char
        elif not quote and char == "{":
            return True, True
        elif not quote and char == "}":
            return True, True
        index += 1
    return bool(quote), expansion or bool(quote)


def _protected_word(raw: str, *, data_operand: bool = False) -> bool:
    brace_group, expansion = _word_syntax(raw)
    if not brace_group and not expansion:
        return False
    chars = _characters(raw)
    if not brace_group:
        # Directory expansions cannot turn a literal/data basename into a
        # protected command. Match only anchored, dynamic basename segments.
        beginning = 0
        for index, (char, _) in enumerate(chars):
            if char == "/":
                beginning = index + 1
        basename = chars[beginning:]
        if not any(char in {"*", _EXPANSION} and active for char, active in basename):
            return False
        if not any(char not in {"*", _EXPANSION} or not active for char, active in basename):
            return False
    if any(active and char in "$`()" for char, active in chars):
        raise _AmbiguousSyntax("Dynamic expansion in brace word")
    return any(_matches(chars, target, data_operand=data_operand) for target in _TARGETS)


def _slot_hazard(raw: str, *, quoted_expansions: bool = True) -> bool:
    return any(
        active and char in "{}$`"
        for char, active in _characters(
            raw, parameter_wildcard=False, quoted_expansions=quoted_expansions
        )
    )


def _command_slots(subject: str, *, strict: bool = False) -> bool:
    """Inspect two CLI positional slots, skipping literal flags and --config.

    Work on token spans rather than a regex grammar. Resolve executable wrappers
    before inspecting CLI slots so another command's operands cannot become heads.
    """
    scan = scan_shell_command(subject)
    words: list[str] = []
    raw: list[str] = []
    for token, (start, end) in zip(scan.tokens, scan.spans, strict=True):
        if not token.quoted and token.value in {"|", "&&", "||", ";", "&", "\n"}:
            if _stage_slots(words, raw, strict=strict):
                return True
            words, raw = [], []
        else:
            words.append(token.value)
            raw.append(subject[start:end])
    return _stage_slots(words, raw, strict=strict)


def _git_alias_stage(words: list[str]) -> bool:
    """Identify in-text Git alias configuration in one bounded token pass."""
    unwrapped = _unwrap(words)
    if not unwrapped or unwrapped[0].rsplit("/", 1)[-1] != "git":
        return False
    prefix = True
    for index, word in enumerate(words):
        if prefix and _ASSIGNMENT.match(word):
            name = word.partition("=")[0]
            if name in {"GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"}:
                return True
            for stem in ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"):
                suffix = name.removeprefix(stem)
                if (
                    name.startswith(stem)
                    and suffix
                    and all(char in "0123456789" for char in suffix)
                ):
                    return True
        else:
            prefix = False
        lowered = word.lower()
        following = words[index + 1].lower() if index + 1 < len(words) else ""
        if (
            ((word == "-c" or lowered == "--config-env") and following.startswith("alias."))
            or (word.startswith("-c") and word[2:].lower().startswith("alias."))
            or lowered.startswith("--config-env=alias.")
        ):
            return True
    return False


def _positional_slots(words: list[str], raw: list[str], index: int) -> bool:
    cursor = index + 1
    slots = 0
    while cursor < len(words) and slots < 2:
        word = words[cursor]
        if _slot_hazard(raw[cursor], quoted_expansions=not word.startswith("--config=")):
            return True
        if word == "--config" and slots == 0:
            cursor += 1
            if cursor < len(words) and _slot_hazard(raw[cursor], quoted_expansions=False):
                return True
        elif not word.startswith("-"):
            slots += 1
        cursor += 1
    return False


def _stage_slots(words: list[str], raw: list[str], *, strict: bool = False) -> bool:
    if _git_alias_stage(words) and any(_slot_hazard(word) for word in raw):
        return True
    # Shell setup can replace ordinary command heads with argv launchers.
    if strict and any(
        _positional_slots(words, raw, index)
        for index, word in enumerate(words)
        if word.rsplit("/", 1)[-1] == "gobby"
        or (word == "gobby.cli" and index > 0 and words[index - 1] == "-m")
    ):
        return True
    pending = [(words, 0)]
    while pending:
        argv, depth = pending.pop()
        if depth > _MAX_DEPTH:
            raise _AmbiguousSyntax("Execution wrapper nesting limit")
        argv = _unwrap(argv)
        if not argv:
            continue
        name = argv[0].rsplit("/", 1)[-1]
        if name == "find":
            # find's action operands execute argv, preserving their raw spelling.
            pending.extend(
                (argv[index + 1 :], depth + 1)
                for index, word in enumerate(argv)
                if word in {"-exec", "-execdir", "-ok", "-okdir"}
            )
        if (
            name == "git"
            and len(argv) <= len(words)
            and argv[1:] == words[len(words) - len(argv) + 1 :]
        ):
            cursor = 1
            while cursor < len(argv):
                word = argv[cursor]
                if word in _GIT_GLOBAL_VALUE_OPTIONS:
                    cursor += 2
                    continue
                if word in _GIT_GLOBAL_FLAGS or any(
                    word.startswith(f"{option}=") for option in _GIT_GLOBAL_VALUE_OPTIONS
                ):
                    cursor += 1
                    continue
                # An expanded subcommand can select an alias that executes argv.
                index = len(words) - len(argv) + cursor
                if _slot_hazard(raw[index]) and any(
                    _positional_slots(words, raw, start)
                    for start in range(index + 1, len(words))
                    if words[start].rsplit("/", 1)[-1] == "gobby"
                    or (words[start] == "gobby.cli" and words[start - 1] == "-m")
                ):
                    return True
                break
        wrapped = _uv_run_argvs(name, argv[1:])
        if wrapped is not None:
            pending.extend((nested, depth + 1) for nested in wrapped)
            continue
        if _python_name(name):
            module = _python_program(argv)[2]
            if module[:1] in (["gobby"], ["gobby.cli"]):
                argv = module
                name = argv[0]
        index = len(words) - len(argv)
        if index < 0 or argv[1:] != words[index + 1 :]:
            # Split-string wrappers can synthesize argv. Keep their conservative
            # protection only when they actually resolve to the Gobby CLI.
            if name in {"gobby", "gobby.cli"} and any(_slot_hazard(word) for word in raw):
                return True
            continue
        literal = "".join(
            char for char, active in _characters(raw[index]) if not (active and char in "{}")
        )
        if literal.rsplit("/", 1)[-1] == "gobby" and _slot_hazard(raw[index]):
            return True
        if name in {"gobby", "gobby.cli"} and _positional_slots(words, raw, index):
            return True
    return False


def _config_operands(subject: str, scan: ShellScan) -> set[tuple[int, int]]:
    """Find config data belonging to a literal gobby simple-command head."""
    operands: set[tuple[int, int]] = set()
    head: bool | None = None
    config_value = False
    redirection_value = False
    for token, span in zip(scan.tokens, scan.spans, strict=True):
        raw = subject[span[0] : span[1]]
        if not token.quoted and token.value in {"|", "&&", "||", ";", "&", "\n"}:
            head, config_value, redirection_value = None, False, False
            continue
        if redirection_value:
            redirection_value = False
            continue
        if is_fd_duplication_token(token):
            continue
        if is_shell_input_redirection_token(token) or is_shell_output_redirection_token(token):
            redirection_value = True
            continue
        if head is None:
            if _ASSIGNMENT.match(raw):
                continue
            head = token.value == "gobby" and not any(_word_syntax(raw))
            continue
        if head:
            if config_value:
                operands.add(span)
                config_value = False
            elif token.value == "--config":
                config_value = True
            elif token.value.startswith("--config="):
                operands.add(span)
    return operands


def _data_operands(subject: str, scan: ShellScan) -> dict[tuple[int, int], int]:
    """Map approved data words to their value offsets; all other words stay strict."""
    operands: dict[tuple[int, int], int] = {}
    head: str | None = None
    redirection_value = False
    git_read_only: bool | None = None
    git_value_pending = False
    for token, span in zip(scan.tokens, scan.spans, strict=True):
        raw = subject[span[0] : span[1]]
        if not token.quoted and token.value in {"|", "&&", "||", ";", "&", "\n"}:
            head, redirection_value, git_read_only, git_value_pending = None, False, None, False
            continue
        if redirection_value:
            redirection_value = False
            continue
        if is_fd_duplication_token(token):
            continue
        if is_shell_input_redirection_token(token) or is_shell_output_redirection_token(token):
            redirection_value = True
            continue
        assignment = _ASSIGNMENT.match(raw)
        if assignment and (head is None or head in _ASSIGNMENT_HEADS):
            operands[span] = assignment.end()
            continue
        if head is None:
            literal = not any(_word_syntax(raw)) and not any(
                active and char in "*?[" for char, active in _characters(raw)
            )
            if literal and not token.quoted and token.value in _COMPOUND_PREFIXES:
                # `do echo` runs `echo`: a compound keyword is not the command word.
                continue
            head = token.value if literal else ""
        elif head == "git":
            # Global options precede the subcommand; a read-only one's later operands are data.
            if git_read_only is not None:
                if git_read_only and not raw.startswith("-"):
                    operands[span] = 0
            elif git_value_pending:
                git_value_pending = False
            elif token.value in _GIT_GLOBAL_VALUE_OPTIONS:
                git_value_pending = True
            elif token.value not in _GIT_GLOBAL_FLAGS and not any(
                token.value.startswith(f"{option}=") for option in _GIT_GLOBAL_VALUE_OPTIONS
            ):
                git_read_only = token.value in _GIT_READ_ONLY_SUBCOMMANDS
        elif head in _DATA_BASENAME_HEADS:
            operands[span] = 0
    return operands


def _shell_setup(text: str, scan: ShellScan, stages: list[list[str]]) -> bool:
    """Shell setup invalidates every data-head assumption in the submitted text."""
    functions = shell_function_bodies(text, scan)
    if functions and functions[1]:
        return True
    if any(token.value.startswith(("BASH_ENV=", "ENV=")) for token in scan.tokens):
        return True
    for words in stages:
        if _git_alias_stage(words):
            return True
        # _unwrap projects eval into sh -c. Preserve that literal head here;
        # the NUL marker cannot be an executable shell command name.
        unwrapped = _unwrap(["\0eval" if word == "eval" else word for word in words])
        if unwrapped and unwrapped[0] in {"alias", "enable", "hash", "source", ".", "\0eval"}:
            return True
    return False


def _approval_subjects(command: str) -> tuple[list[tuple[str, ShellScan]], bool]:
    """Preflight all bodies before choosing any data exemptions."""
    subjects: list[tuple[str, ShellScan]] = []
    strict = False
    pending = [(command, 0)]
    seen: set[str] = set()
    while pending:
        text, depth = pending.pop()
        if text in seen:
            continue
        seen.add(text)
        if depth > _MAX_DEPTH:
            raise _AmbiguousSyntax("Shell nesting limit")
        scan = scan_shell_command(text)
        strict |= _shell_setup(text, scan, _decoded_stages(text))
        # A Python heredoc body is source, not shell words. Its braces are code, so
        # only the shell-expanded substitutions inside it stay subjects.
        for subject in executable_command_subjects(text, python_bodies="data"):
            scan = scan_shell_command(subject)
            stages = _decoded_stages(subject)
            strict |= _shell_setup(subject, scan, stages)
            scripts = _wrapper_scripts(stages)
            scripts.extend(_shell_input_scripts(subject))
            pending.extend((script, depth + 1) for script in scripts)
            # Correlated subjects retain substitution bodies with spaces.
            # Inspect their owner as one word; executable bodies already
            # have independent subjects from executable_command_subjects.
            if "$(" in subject or "`" in subject:
                masked, _ = command_substitution_bodies(subject, scan.spans, depth=depth)
                if masked != subject:
                    subject = masked
                    scan = scan_shell_command(subject)
            subjects.append((subject, scan))
    return subjects, strict


def _approval_spelling(command: object, *, command_slots: bool) -> bool:
    """Refuse an unquoted brace word capable of producing a protected basename.

    Reuse executable subjects and wrapper script extraction, keeping the shared
    matcher expansion-free. Unsupported ambiguous brace syntax fails closed.
    """
    if not isinstance(command, str):
        return False
    if not command_slots and not any(char in command for char in "{}"):
        return False
    try:
        subjects, strict = _approval_subjects(command)
        for subject, scan in subjects:
            if command_slots and _command_slots(subject, strict=strict):
                return True
            config_operands = set() if strict else _config_operands(subject, scan)
            data_operands = {} if strict else _data_operands(subject, scan)
            owner, _ = process_substitution_bodies(
                scan.tokens,
                {id(token): span for token, span in zip(scan.tokens, scan.spans, strict=True)},
                subject,
            )
            owner_ids = {id(token) for token in owner}
            if any(
                _protected_word(
                    subject[start + data_operands.get((start, end), 0) : end],
                    data_operand=(start, end) in data_operands,
                )
                for token, (start, end) in zip(scan.tokens, scan.spans, strict=True)
                if id(token) in owner_ids and (start, end) not in config_operands
            ):
                return True
    except (ValueError, RecursionError):
        return True
    return False


def approval_brace_spelling(command: object) -> bool:
    """Refuse protected brace words independently of their argv launcher."""
    return _approval_spelling(command, command_slots=False)


def approval_command_word_expansion(command: object) -> bool:
    """Bounded companion to the unchanged literal approval command pattern."""
    return _approval_spelling(command, command_slots=True)
