"""Rust cfg(test) test-writing evidence for the TDD gate.

Rust unit tests live either in `<module>/tests.rs` files (recognized by the
shared path classifier) or in inline `#[cfg(test)]` modules of production
files. This module inspects Edit/Write tool input for a `.rs` path and
reports whether the change is test writing:

- the changed text introduces a cfg-gated test item (it carries a
  `#[cfg(...test...)]` attribute), or
- the change lies entirely within an existing `#[cfg(test)]` module block
  of the file on disk.

Rust edits outside such blocks stay production writes. The within-block
check locates the edit region in the on-disk file through either side of
the old/new text pair, so it works both before a write (old text present)
and after it (new text present).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

_NEW_TEXT_KEYS = ("new_str", "new_string", "newStr", "new_text", "newText", "replacement")
_OLD_TEXT_KEYS = ("old_str", "old_string", "oldStr", "old_text", "oldText", "old")
_CONTENT_KEYS = ("content",)
_NESTED_EDIT_KEYS = ("edits", "changes")
_EDIT_PATH_KEYS = ("file_path", "path", "notebook_path", "filePath")

_CFG_ATTR_RE = re.compile(r"#\[\s*cfg\s*\(")
_RAW_STRING_OPEN_RE = re.compile(r"b?r(#*)\"")
_MODULE_ITEM_RE = re.compile(r"(?:pub\s*(?:\([^)]*\))?\s*)?mod\s+[A-Za-z_][A-Za-z0-9_]*\s*\{")
_TEST_TOKEN_RE = re.compile(r"\btest\b")
_NEGATED_TEST_RE = re.compile(r"not\s*\(\s*$")


def rust_edit_is_test_writing(path: str, tool_input: Any) -> bool:
    """Whether an Edit/Write to a Rust file counts as test writing for the TDD gate."""
    if not path.casefold().endswith(".rs"):
        return False
    pairs = list(_iter_edit_pairs(tool_input, path))
    if not pairs:
        return False
    if any(new is not None and _contains_cfg_test_attribute(new) for _, new in pairs):
        return True
    disk_text = _read_text_best_effort(path)
    if disk_text is None:
        return False
    spans = _cfg_test_module_spans(_mask_rust_noncode(disk_text))
    if not spans:
        return False
    for old_text, new_text in pairs:
        region = _edit_region(disk_text, old_text, new_text)
        if region is None or not _region_within(region, spans):
            return False
    return True


def _iter_edit_pairs(value: Any, path: str) -> Iterator[tuple[str | None, str]]:
    """Yield (old text, new text) pairs from edit input scoped to one file path."""
    if isinstance(value, Mapping):
        if not _mapping_targets_path(value, path):
            return
        old_text = _first_text(value, _OLD_TEXT_KEYS)
        new_text = _first_text(value, (*_NEW_TEXT_KEYS, *_CONTENT_KEYS))
        if new_text is not None:
            yield old_text, new_text
        for key in _NESTED_EDIT_KEYS:
            nested = value.get(key)
            if isinstance(nested, list | tuple):
                for item in nested:
                    yield from _iter_edit_pairs(item, path)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _iter_edit_pairs(item, path)


def _mapping_targets_path(value: Mapping[str, Any], path: str) -> bool:
    """Whether a mapping's own path key, if any, refers to the checked path."""
    for key in _EDIT_PATH_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return _paths_refer_to_same_file(candidate, path)
    return True


def _first_text(value: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        text = value.get(key)
        if isinstance(text, str) and text:
            return text
    return None


def _paths_refer_to_same_file(edit_path: str, path: str) -> bool:
    left = edit_path.replace("\\", "/")
    right = path.replace("\\", "/")
    return left == right or left.endswith(f"/{right}") or right.endswith(f"/{left}")


def _contains_cfg_test_attribute(text: str) -> bool:
    masked = _mask_rust_noncode(text)
    for match in _CFG_ATTR_RE.finditer(masked):
        close = _matching_delimiter(masked, match.end() - 1, "(", ")")
        if close is None:
            continue
        if _predicate_gates_test(masked[match.end() : close]):
            return True
    return False


def _cfg_test_module_spans(masked: str) -> list[tuple[int, int]]:
    """Start-inclusive, end-exclusive spans of cfg-test module blocks in masked text."""
    spans: list[tuple[int, int]] = []
    for match in _CFG_ATTR_RE.finditer(masked):
        close_paren = _matching_delimiter(masked, match.end() - 1, "(", ")")
        if close_paren is None:
            continue
        if not _predicate_gates_test(masked[match.end() : close_paren]):
            continue
        bounds = _module_block_bounds(masked, close_paren + 1)
        if bounds is None:
            continue
        spans.append((match.start(), bounds[1]))
    return spans


def _module_block_bounds(masked: str, position: int) -> tuple[int, int] | None:
    """Bounds of the braces of the mod item attached to a cfg attribute."""
    index = _skip_blanks(masked, position)
    if not masked.startswith("]", index):
        return None
    index += 1
    while True:
        attribute_start = _skip_blanks(masked, index)
        if not masked.startswith("#[", attribute_start):
            index = attribute_start
            break
        attribute_end = masked.find("]", attribute_start + 2)
        if attribute_end == -1:
            return None
        index = attribute_end + 1
    item = _MODULE_ITEM_RE.match(masked, index)
    if item is None:
        return None
    block_start = item.end() - 1
    block_end = _matching_delimiter(masked, block_start, "{", "}")
    if block_end is None:
        return None
    return block_start, block_end + 1


def _skip_blanks(masked: str, index: int) -> int:
    while index < len(masked) and masked[index].isspace():
        index += 1
    return index


def _predicate_gates_test(predicate: str) -> bool:
    """Whether a cfg predicate enables code for test builds; `not(test)` does not."""
    for match in _TEST_TOKEN_RE.finditer(predicate):
        if _NEGATED_TEST_RE.search(predicate[: match.start()]):
            continue
        return True
    return False


def _matching_delimiter(
    text: str,
    open_index: int,
    open_char: str,
    close_char: str,
) -> int | None:
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == open_char:
            depth += 1
        elif text[index] == close_char:
            depth -= 1
            if depth == 0:
                return index
    return None


def _edit_region(
    disk_text: str,
    old_text: str | None,
    new_text: str | None,
) -> tuple[int, int] | None:
    """Locate the edited region in on-disk text through either side of the pair."""
    for fragment in (old_text, new_text):
        if fragment:
            offset = disk_text.find(fragment)
            if offset != -1:
                return offset, offset + len(fragment)
    return None


def _region_within(region: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    start, end = region
    return any(span_start <= start and end <= span_end for span_start, span_end in spans)


def _mask_rust_noncode(text: str) -> str:
    """Blank Rust strings and comments with spaces, preserving text offsets."""
    chars = list(text)
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "/" and text.startswith("//", index):
            end = text.find("\n", index)
            end = length if end == -1 else end
        elif char == "/" and text.startswith("/*", index):
            end = _block_comment_end(text, index)
        elif raw_string := _RAW_STRING_OPEN_RE.match(text, index):
            closing = '"' + raw_string.group(1)
            end = text.find(closing, raw_string.end())
            end = length if end == -1 else end + len(closing)
        elif char == '"':
            end = _cooked_string_end(text, index)
        elif char == "'":
            literal_end = _char_literal_end(text, index)
            if literal_end is None:
                index += 1
                continue
            end = literal_end
        else:
            index += 1
            continue
        _blank(chars, index, end)
        index = end
    return "".join(chars)


def _block_comment_end(text: str, start: int) -> int:
    depth = 1
    index = start + 2
    while index < len(text):
        if text.startswith("/*", index):
            depth += 1
            index += 2
        elif text.startswith("*/", index):
            depth -= 1
            index += 2
            if depth == 0:
                return index
        else:
            index += 1
    return len(text)


def _cooked_string_end(text: str, start: int) -> int:
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index + 1
        index += 1
    return len(text)


def _char_literal_end(text: str, start: int) -> int | None:
    """End of a char literal, or None for lifetimes such as `'static`."""
    if text.startswith("\\", start + 1):
        index = start + 1
        while index < len(text):
            char = text[index]
            if char == "\\":
                index += 2
                continue
            if char == "'":
                return index + 1
            index += 1
        return len(text)
    if start + 2 < len(text) and text[start + 2] == "'":
        return start + 3
    return None


def _blank(chars: list[str], start: int, end: int) -> None:
    clamped = min(end, len(chars))
    chars[start:clamped] = [" "] * (clamped - start)


def _read_text_best_effort(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
