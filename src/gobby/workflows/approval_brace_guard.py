"""Recognize protected brace spellings without generating shell expansions.

Arbitrary argv launchers make command-position inference unsafe. Match the two
protected basenames anywhere, retaining quote provenance and ordinary brace data.
Each alternative carries at most len(target) + 1 offsets, never expanded strings.
"""

from __future__ import annotations

import fnmatch
import re

from gobby.hooks._normalization_shell import scan_shell_command

from .engine.command_matching import (
    _decoded_stages,
    _shell_input_scripts,
    _wrapper_scripts,
    executable_command_subjects,
)

_TARGETS = ("gobby", "approve-dirty-paths")
_MAX_DEPTH = 32
type _Character = tuple[str, bool]


class _AmbiguousSyntax(ValueError):
    """An unsupported expansion cannot establish that a brace word is safe."""


def _parameter_end(raw: str, index: int) -> int:
    """Find a simple parameter span; nested expansion remains ambiguous."""
    quote = ""
    index += 2
    while index < len(raw):
        char = raw[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote != "'" and (char in "{`" or raw.startswith("$(", index)):
            raise _AmbiguousSyntax("Nested expansion in parameter span")
        if char == quote:
            quote = ""
        elif not quote and char in "'\"":
            quote = char
        elif not quote and char == "}":
            return index + 1
        index += 1
    raise _AmbiguousSyntax("Unclosed parameter span")


def _characters(raw: str) -> list[_Character]:
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
        if quote != "'" and raw.startswith("${", index):
            index = _parameter_end(raw, index)
            result.append(("*", True))
            continue
        if char == quote:
            quote = ""
        elif not quote and char in "'\"":
            quote = char
        else:
            result.append((char, not quote or (quote == '"' and char in "$`")))
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


def _protected_word(raw: str) -> bool:
    chars = _characters(raw)
    if not any(active and char in "{}" for char, active in chars):
        return False
    if any(active and char in "$`()" for char, active in chars):
        raise _AmbiguousSyntax("Dynamic expansion in brace word")
    return any(_matches(chars, target) for target in _TARGETS)


def approval_brace_spelling(command: object) -> bool:
    """Refuse an unquoted brace word capable of producing a protected basename.

    Reuse executable subjects and wrapper script extraction, keeping the shared
    matcher expansion-free. Unsupported ambiguous brace syntax fails closed.
    """
    if not isinstance(command, str) or not any(char in command for char in "{}"):
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
                scan = scan_shell_command(subject)
                if any(_protected_word(subject[start:end]) for start, end in scan.spans):
                    return True
                scripts = _wrapper_scripts(_decoded_stages(subject))
                scripts.extend(_shell_input_scripts(subject))
                pending.extend((script, depth + 1) for script in scripts)
    except (ValueError, RecursionError):
        return True
    return False
