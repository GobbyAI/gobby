"""Recognize protected brace spellings without generating shell expansions.

Arbitrary argv launchers make command-position inference unsafe. Match the two
protected basenames anywhere, retaining quote provenance and ordinary brace data.
Each alternative carries at most len(target) + 1 offsets, never expanded strings.
"""

from __future__ import annotations

import fnmatch
import re

from gobby.hooks._ansi_c import opens_ansi_c_quote
from gobby.hooks._normalization_compound import command_substitution_bodies
from gobby.hooks._normalization_shell import scan_shell_command
from gobby.hooks._shell_execution_words import _unwrap

from .engine.command_matching import (
    _decoded_stages,
    _shell_input_scripts,
    _uv_run_argvs,
    _wrapper_scripts,
    executable_command_subjects,
)

_TARGETS = ("gobby", "approve-dirty-paths")
_MAX_DEPTH = 32
_DATA_COMMANDS = frozenset(
    {"echo", "printf", "rg", "grep", "gcode", "cat", "ls", "wc", "git", "cp"}
)
type _Character = tuple[str, bool]


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
            result.append(("*" if parameter_wildcard else "$", not quote or quoted_expansions))
            continue
        if (
            quote != "'"
            and char == "$"
            and index + 1 < len(raw)
            and raw[index + 1] in "0123456789@*#?$!-"
        ):
            index += 2
            result.append(("*" if parameter_wildcard else "$", not quote or quoted_expansions))
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
            result.append(("*" if parameter_wildcard else "$", not quote or quoted_expansions))
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


def _matches(chars: list[_Character], target: str) -> bool:
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


def _protected_word(raw: str) -> bool:
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
        if not any(char == "*" and active for char, active in basename):
            return False
        if not any(char != "*" or not active for char, active in basename):
            return False
    if any(active and char in "$`()" for char, active in chars):
        raise _AmbiguousSyntax("Dynamic expansion in brace word")
    return any(_matches(chars, target) for target in _TARGETS)


def _slot_hazard(raw: str, *, quoted_expansions: bool = True) -> bool:
    return any(
        active and char in "{}$`"
        for char, active in _characters(
            raw, parameter_wildcard=False, quoted_expansions=quoted_expansions
        )
    )


def _command_slots(subject: str) -> bool:
    """Inspect two CLI positional slots, skipping literal flags and --config.

    Work on token spans rather than a regex grammar. Scanning all argv positions
    also covers unknown argv launchers without maintaining a wrapper grammar.
    Quoted prose stays one data word and cannot become CLI positional slots.
    """
    scan = scan_shell_command(subject)
    words: list[str] = []
    raw: list[str] = []
    for token, (start, end) in zip(scan.tokens, scan.spans, strict=True):
        if not token.quoted and token.value in {"|", "&&", "||", ";", "&", "\n"}:
            if _stage_slots(words, raw):
                return True
            words, raw = [], []
        else:
            words.append(token.value)
            raw.append(subject[start:end])
    return _stage_slots(words, raw)


def _stage_slots(words: list[str], raw: list[str]) -> bool:
    unwrapped = _unwrap(words)
    if unwrapped:
        name = unwrapped[0].rsplit("/", 1)[-1]
        argvs = _uv_run_argvs(name, unwrapped[1:])
        heads = [argv[0].rsplit("/", 1)[-1] for argv in argvs or [unwrapped] if argv]
        if heads and all(head in _DATA_COMMANDS for head in heads):
            return False
    checked_until = 0
    for index, word in enumerate(words):
        if index < checked_until:
            continue
        name = word.rsplit("/", 1)[-1]
        literal = "".join(
            char for char, active in _characters(raw[index]) if not (active and char in "{}")
        )
        if literal.rsplit("/", 1)[-1] == "gobby" and _slot_hazard(raw[index]):
            return True
        if name != "gobby" and not (name == "gobby.cli" and index > 0 and words[index - 1] == "-m"):
            continue
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
        # --config values may themselves be named gobby. They are operands of
        # this invocation, so do not rescan every remaining suffix from them.
        checked_until = cursor
    return False


def _approval_spelling(command: object, *, command_slots: bool) -> bool:
    """Refuse an unquoted brace word capable of producing a protected basename.

    Reuse executable subjects and wrapper script extraction, keeping the shared
    matcher expansion-free. Unsupported ambiguous brace syntax fails closed.
    """
    if not isinstance(command, str):
        return False
    if not command_slots and not any(char in command for char in "{}"):
        return False
    pending = [(command, 0)]
    seen: set[str] = set()
    try:
        while pending:
            text, depth = pending.pop()
            if text in seen:
                continue
            seen.add(text)
            if depth > _MAX_DEPTH:
                raise _AmbiguousSyntax("Shell nesting limit")
            for subject in executable_command_subjects(text):
                # Correlated subjects retain substitution bodies with spaces.
                # Inspect their owner as one word; executable bodies already
                # have independent subjects from executable_command_subjects.
                scan = scan_shell_command(subject)
                if "$(" in subject or "`" in subject:
                    masked, _ = command_substitution_bodies(subject, scan.spans, depth=depth)
                    if masked != subject:
                        subject = masked
                        scan = scan_shell_command(subject)
                if command_slots and _command_slots(subject):
                    return True
                if any(_protected_word(subject[start:end]) for start, end in scan.spans):
                    return True
                scripts = _wrapper_scripts(_decoded_stages(subject))
                scripts.extend(_shell_input_scripts(subject))
                pending.extend((script, depth + 1) for script in scripts)
    except (ValueError, RecursionError):
        return True
    return False


def approval_brace_spelling(command: object) -> bool:
    """Refuse protected brace words independently of their argv launcher."""
    return _approval_spelling(command, command_slots=False)


def approval_command_word_expansion(command: object) -> bool:
    """Bounded companion to the unchanged literal approval command pattern."""
    return _approval_spelling(command, command_slots=True)
