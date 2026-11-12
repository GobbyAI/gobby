"""Bounded path-pattern expansion and lexical/canonical path resolution."""

import os
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from gobby.hooks._path_scope import resolve_tool_path

READ_PATTERN = re.compile(r"[*?\[]")
EXTGLOB = re.compile(r"[@+?!*]\(")
_BRACE_RANGE = re.compile(r"(?:-?\d+\.\.-?\d+|[a-zA-Z]\.\.[a-zA-Z])(?:\.\.-?\d+)?")
_READ_EXPANSION_LIMIT = 256


def brace_read_words(raw: str) -> list[str] | None:
    """Expand bounded comma alternatives; approximate ranges with a filename glob.

    Non-expanding braces remain literal, including awk/sed program data. Work on
    complete words before path normalization because alternatives can contain '/'
    or combine separate dot fragments into '..'. None means the bound was exceeded.
    """
    pending = [raw]
    expanded: list[str] = []
    budget = _READ_EXPANSION_LIMIT
    while pending:
        word = pending.pop()
        budget -= 1
        if budget < 0:
            return None
        groups: list[list[int]] = []
        for end, char in enumerate(word):
            if char == "{":
                groups.append([end])
            elif char == "," and groups:
                groups[-1].append(end)
            elif char == "}" and groups:
                positions = groups.pop()
                start = positions[0]
                if len(positions) > 1:
                    bounds = [*positions, end]
                    alternatives = [
                        word[a + 1 : b] for a, b in zip(bounds, bounds[1:], strict=False)
                    ]
                elif _BRACE_RANGE.fullmatch(word[start + 1 : end]):
                    alternatives = ["*"]
                else:
                    continue
                if len(pending) + len(alternatives) > budget:
                    return None
                pending.extend(word[:start] + choice + word[end + 1 :] for choice in alternatives)
                break
        else:
            expanded.append(word)
    return expanded


def extglob_read_word(word: str) -> str | None:
    """Overapproximate filename extglobs without executing a shell.

    Slash-bearing groups change path structure and cannot be inspected as one
    component. Refuse those rather than silently matching only part of a group.
    """
    while match := EXTGLOB.search(word):
        depth = 1
        end = match.end()
        while end < len(word) and depth:
            depth += (word[end] == "(") - (word[end] == ")")
            end += 1
        if depth or "/" in word[match.end() : end]:
            return None
        word = word[: match.start()] + "*" + word[end:]
    return word


def resolve_read_target(raw: Any, base: Path | None) -> tuple[Path, int | None] | None:
    """Resolve a pattern and retain where expansion starts; base paths stay literal."""
    if not isinstance(raw, str) or not raw:
        return None
    parts = Path(os.path.expandvars(os.path.expanduser(raw))).parts
    for index, part in enumerate(parts):
        if READ_PATTERN.search(part):
            prefix = resolve_tool_path(str(Path(*parts[:index])), base)
            return (
                (prefix.joinpath(*parts[index:]), len(prefix.parts)) if prefix is not None else None
            )
    literal = resolve_tool_path(raw, base)
    return (literal, None) if literal is not None else None


def shell_dot_read_words(word: str, pattern: str) -> list[str] | None:
    """Include shell dot/parent matches without enumerating filesystem entries."""
    originals = Path(word).parts
    variants = [Path(pattern).parts]
    for index, part in enumerate(originals):
        if not (part.startswith(".") or re.search(r"[|(]\.", part)):
            continue
        for name in (".", ".."):
            if part == name or not read_pattern_matches(name, variants[0][index]):
                continue
            variants.extend(parts[:index] + (name,) + parts[index + 1 :] for parts in variants[:])
            if len(variants) > _READ_EXPANSION_LIMIT:
                return None
    return [os.path.join(*parts) for parts in variants]


def lexical_read_target(
    raw: str, base: Path | None, *, pattern: bool
) -> tuple[Path, int | None] | None:
    target = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not target.is_absolute():
        if base is None:
            return None
        target = base / target
    target = Path(os.path.normpath(target))
    start = (
        next((index for index, part in enumerate(target.parts) if READ_PATTERN.search(part)), None)
        if pattern
        else None
    )
    return target, start


def read_pattern_matches(name: str, pattern: str) -> bool:
    """Conservatively include shell bracket classes and Unicode/case variants."""
    pattern = re.sub(r"\[.*\]", "*", pattern)
    return fnmatchcase(name, pattern) or fnmatchcase(name.casefold(), pattern.casefold())


def pattern_reaches_root(parts: tuple[str, ...], root_parts: tuple[str, ...]) -> bool:
    """Whether a pattern has enough matching components to enter the workspace root."""
    pending = [(0, 0)]
    visited: set[tuple[int, int]] = set()
    while pending:
        index, depth = pending.pop()
        if depth == len(root_parts):
            return True
        if index == len(parts) or (index, depth) in visited:
            continue
        visited.add((index, depth))
        if parts[index] == "**":
            pending.extend(((index + 1, depth), (index, depth + 1)))
        elif read_pattern_matches(root_parts[depth], parts[index]):
            pending.append((index + 1, depth + 1))
    return False
