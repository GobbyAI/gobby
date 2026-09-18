"""Rust cfg(test) evidence for the TDD gate.

Rust unit tests live either in `<module>/tests.rs` files, which the shared path
classifier recognizes, or in `#[cfg(test)]` items of a production file. This
module inspects structured Edit/Write input for a `.rs` path and reports
whether the change touches test code only.

The classifier compares the file text before and after the change with every
cfg(test) item cut out. When the remaining production text is unchanged, the
change either introduced a cfg(test) item or landed inside one; anything that
also rewrites production text stays a production write.

The before/after pair comes from the file on disk, so the check works before a
write (the old text is still there) and after it (the new text is). Only
absolute paths are read: a relative path cannot be resolved to one checkout
from inside the daemon, so such a change stays a production write. Patch-text
payloads carry no old/new pair and stay production writes too.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_NEW_TEXT_KEYS = ("new_str", "new_string", "newStr", "new_text", "newText", "replacement")
_OLD_TEXT_KEYS = ("old_str", "old_string", "oldStr", "old_text", "oldText", "old")
_CONTENT_KEYS = ("content",)
_REPLACE_ALL_KEYS = ("replace_all", "replaceAll")
_NESTED_EDIT_KEYS = ("edits", "changes")
_EDIT_PATH_KEYS = ("file_path", "path", "notebook_path", "filePath")

_CFG_ATTR_RE = re.compile(r"#\[\s*cfg\s*\(")
_RAW_STRING_OPEN_RE = re.compile(r"b?r(#*)\"")
_TEST_TOKEN_RE = re.compile(r"\btest\b")
_NEGATED_TEST_RE = re.compile(r"not\s*\(\s*$")


@dataclass(frozen=True)
class _RustChange:
    """One structured change to a Rust file: an edit pair or a full-content write."""

    old: str | None
    new: str
    replace_all: bool


def rust_edit_is_test_writing(path: str, tool_input: Any) -> bool:
    """Whether an Edit/Write to a Rust file changes cfg(test) code only."""
    if not path.casefold().endswith(".rs"):
        return False
    changes = list(_iter_changes(tool_input, path))
    if not changes:
        return False
    on_disk = _read_absolute_text(path)
    return all(_change_is_test_only(on_disk, change) for change in changes)


def _change_is_test_only(on_disk: str | None, change: _RustChange) -> bool:
    texts = _before_and_after(on_disk, change)
    if texts is None:
        return False
    before, after = texts
    if before == after:
        # A full-content write whose text already landed: the prior text is
        # gone, so credit the cfg(test) code the written file carries.
        return bool(_cfg_test_spans(after))
    return _production_signature(before) == _production_signature(after)


def _before_and_after(on_disk: str | None, change: _RustChange) -> tuple[str, str] | None:
    """File text on both sides of one change, or None when it cannot be located."""
    if change.old is None:
        return (on_disk if on_disk is not None else ""), change.new
    if on_disk is None:
        return None
    if change.old in on_disk:
        return on_disk, _replace(on_disk, change.old, change.new, change.replace_all)
    if change.new in on_disk:
        return _replace(on_disk, change.new, change.old, change.replace_all), on_disk
    return None


def _replace(text: str, old: str, new: str, replace_all: bool) -> str:
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def _production_signature(text: str) -> str:
    """Non-whitespace text outside every cfg(test) item."""
    pieces: list[str] = []
    cursor = 0
    for start, end in _cfg_test_spans(text):
        if start < cursor:
            continue
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return "".join("".join(pieces).split())


def _cfg_test_spans(text: str) -> list[tuple[int, int]]:
    """Start-inclusive, end-exclusive spans of the cfg(test) items in Rust text."""
    masked = _mask_rust_noncode(text)
    spans: list[tuple[int, int]] = []
    for match in _CFG_ATTR_RE.finditer(masked):
        close_paren = _matching_delimiter(masked, match.end() - 1, "(", ")")
        if close_paren is None:
            continue
        if not _predicate_gates_test(masked[match.end() : close_paren]):
            continue
        end = _item_end(masked, close_paren + 1)
        if end is not None:
            spans.append((match.start(), end))
    return spans


def _item_end(masked: str, position: int) -> int | None:
    """End of the item a cfg attribute gates, skipping the attributes before it."""
    index = _skip_blanks(masked, position)
    if not masked.startswith("]", index):
        return None
    index += 1
    while True:
        index = _skip_blanks(masked, index)
        if not masked.startswith("#[", index):
            break
        attribute_end = _matching_delimiter(masked, index + 1, "[", "]")
        if attribute_end is None:
            return None
        index = attribute_end + 1
    return _item_terminator(masked, index)


def _item_terminator(masked: str, index: int) -> int | None:
    """End of one Rust item: its brace block, or the `;` that closes it."""
    while index < len(masked):
        char = masked[index]
        if char == ";":
            return index + 1
        if char == "{":
            block_end = _matching_delimiter(masked, index, "{", "}")
            if block_end is None:
                return None
            # `use a::{b, c};` keeps a terminator after its brace group.
            tail = _skip_blanks(masked, block_end + 1)
            return tail + 1 if masked.startswith(";", tail) else block_end + 1
        index += 1
    return None


def _predicate_gates_test(predicate: str) -> bool:
    """Whether a cfg predicate enables code for test builds; `not(test)` does not."""
    for match in _TEST_TOKEN_RE.finditer(predicate):
        if _NEGATED_TEST_RE.search(predicate[: match.start()]):
            continue
        return True
    return False


def _matching_delimiter(text: str, open_index: int, open_char: str, close_char: str) -> int | None:
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == open_char:
            depth += 1
        elif text[index] == close_char:
            depth -= 1
            if depth == 0:
                return index
    return None


def _skip_blanks(masked: str, index: int) -> int:
    while index < len(masked) and masked[index].isspace():
        index += 1
    return index


def _iter_changes(value: Any, path: str) -> Iterator[_RustChange]:
    """Yield the structured changes in edit input that target one file path."""
    if isinstance(value, Mapping):
        if not _mapping_targets_path(value, path):
            return
        new_text = _first_text(value, (*_NEW_TEXT_KEYS, *_CONTENT_KEYS))
        if new_text is not None:
            yield _RustChange(
                old=_first_text(value, _OLD_TEXT_KEYS),
                new=new_text,
                replace_all=_replace_all_requested(value),
            )
        for key in _NESTED_EDIT_KEYS:
            nested = value.get(key)
            if isinstance(nested, list | tuple):
                for item in nested:
                    yield from _iter_changes(item, path)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _iter_changes(item, path)


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


def _replace_all_requested(value: Mapping[str, Any]) -> bool:
    return any(value.get(key) is True for key in _REPLACE_ALL_KEYS)


def _paths_refer_to_same_file(edit_path: str, path: str) -> bool:
    left = edit_path.replace("\\", "/")
    right = path.replace("\\", "/")
    return left == right or left.endswith(f"/{right}") or right.endswith(f"/{left}")


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


def _read_absolute_text(path: str) -> str | None:
    candidate = Path(path)
    if not candidate.is_absolute():
        return None
    try:
        return candidate.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
