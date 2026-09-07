"""Recognize function-style invocations of crate-local test macros statically.

A unique local definition containing #[test] makes its invocation's function
items test candidates. External/path-qualified macros are not inferred. This
does not attempt arbitrary macro expansion or evaluate conditional compilation.
"""

from __future__ import annotations

import re
from pathlib import Path

from gobby.test_quality._analyzer_scanner import (
    _find_matching_delimiter,
    _rust_char_literal_end,
    _rust_lifetime_end,
    _rust_raw_string_end,
)

_DEFINITION_RE = re.compile(r"\bmacro_rules\s*!\s*(?P<name>\w+)\s*(?P<open>[({\[])")
_INVOCATION_RE = re.compile(r"(?P<name>\b\w+(?:\s*::\s*\w+)*)\s*!\s*(?P<open>[({\[])")
_TEST_ATTRIBUTE_RE = re.compile(r"#\s*\[\s*test\s*\]")
_CLOSERS = {"(": ")", "{": "}", "[": "]"}


def _blank(source: str) -> str:
    return "".join("\n" if char == "\n" else " " for char in source)


def _rust_code(source: str) -> str:
    """Mask comments and literals, retaining source offsets and line numbers."""
    parts: list[str] = []
    index = 0
    while index < len(source):
        start = index
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            index = len(source) if end == -1 else end
        elif source.startswith("/*", index):
            depth = 1
            index += 2
            while index < len(source) and depth:
                if source.startswith("/*", index):
                    depth += 1
                    index += 2
                elif source.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
        else:
            raw_end = _rust_raw_string_end(source, index)
            char_end = _rust_char_literal_end(source, index) if source[index] == "'" else None
            if raw_end is not None:
                index = raw_end + 1
            elif char_end is not None:
                index = char_end + 1
            elif source[index] == '"':
                index += 1
                while index < len(source):
                    if source[index] == "\\":
                        index += 2
                    elif source[index] == '"':
                        index += 1
                        break
                    else:
                        index += 1
            else:
                lifetime_end = _rust_lifetime_end(source, index) if source[index] == "'" else None
                index = lifetime_end if lifetime_end is not None else index + 1
                parts.append(
                    _blank(source[start:index]) if lifetime_end is not None else source[start:index]
                )
                continue
        parts.append(_blank(source[start:index]))
    return "".join(parts)


def _macro_definitions(code: str) -> list[tuple[str, int, int, bool]]:
    definitions: list[tuple[str, int, int, bool]] = []
    cursor = 0
    while match := _DEFINITION_RE.search(code, cursor):
        opener = match.start("open")
        end = _find_matching_delimiter(code, opener, code[opener], _CLOSERS[code[opener]])
        if end is None:
            break
        definitions.append(
            (
                match["name"],
                match.start(),
                end + 1,
                bool(_TEST_ATTRIBUTE_RE.search(code, opener, end)),
            )
        )
        cursor = end + 1
    return definitions


def _rust_macro_context(
    source: str, test_macros: frozenset[str]
) -> tuple[str, list[tuple[int, int]]]:
    code = _rust_code(source)
    for _, start, end, _ in reversed(_macro_definitions(code)):
        code = code[:start] + _blank(code[start:end]) + code[end:]
    ranges: list[tuple[int, int]] = []
    for match in _INVOCATION_RE.finditer(code):
        if match["name"] not in test_macros:
            continue
        opener = match.start("open")
        close = _find_matching_delimiter(code, opener, code[opener], _CLOSERS[code[opener]])
        if close is not None:
            ranges.append((opener, close))
    return code, ranges


class _RustMacroResolver:
    """Cache definitions for one audit, never retaining source between audits."""

    def __init__(self) -> None:
        self._cache: dict[Path, frozenset[str]] = {}

    def for_file(self, path: Path, source: str) -> frozenset[str]:
        path = path.resolve()
        crate = next((parent for parent in path.parents if (parent / "Cargo.toml").is_file()), None)
        if crate is None:
            # Standalone snippets can establish definitions only in their own source.
            return self._test_names([source])
        if crate not in self._cache:
            sources: list[str] = []
            for directory, dirnames, filenames in crate.walk():
                dirnames[:] = [
                    name
                    for name in dirnames
                    if not name.startswith(".")
                    and name not in {"target", "vendor"}
                    and not (directory / name / "Cargo.toml").is_file()
                ]
                for name in filenames:
                    candidate = directory / name
                    if candidate.suffix == ".rs" and candidate.resolve().is_relative_to(crate):
                        sources.append(
                            source if candidate == path else candidate.read_text(encoding="utf-8")
                        )
            self._cache[crate] = self._test_names(sources)
        return self._cache[crate]

    @staticmethod
    def _test_names(sources: list[str]) -> frozenset[str]:
        definitions: dict[str, list[bool]] = {}
        for source in sources:
            for name, _, _, emits_test in _macro_definitions(_rust_code(source)):
                definitions.setdefault(name, []).append(emits_test)
        # Ambiguous/shadowed names cannot safely establish a test macro statically.
        return frozenset(name for name, candidates in definitions.items() if candidates == [True])
