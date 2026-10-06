"""Tests for production-size primitives and the typed delete-lines proof."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Any

import pytest

from gobby.plans.production_size import (
    DeleteLinesProof,
    DeleteLinesProofError,
    git_blob_id,
    parse_delete_lines_proof,
    production_line_count,
    project_delete_lines,
)

pytestmark = pytest.mark.unit

BLOB = "0123456789abcdef0123456789abcdef01234567"
CANONICAL = (
    f"`src/app/mod.rs::*` — operation: delete-lines — base-blob: {BLOB} — lines: 1, 3-4"
    " — scope-reason: remove the re-export"
)
PARSED = DeleteLinesProof(path="src/app/mod.rs", base_blob=BLOB, ranges=((1, 1), (3, 4)))


def _variant(old: str, new: str) -> str:
    return "- " + CANONICAL.replace(old, new, 1)


_REJECTED = {
    "missing-key": _variant(f" — base-blob: {BLOB}", ""),
    "repeated-key": _variant(" — lines: 1, 3-4", " — lines: 1 — lines: 3-4"),
    "reordered-keys": _variant(
        f"base-blob: {BLOB} — lines: 1, 3-4", f"lines: 1, 3-4 — base-blob: {BLOB}"
    ),
    "backticked-metadata": _variant(f"base-blob: {BLOB}", f"base-blob: `{BLOB}`"),
    "uppercase-hex": _variant(BLOB, BLOB.upper()),
    "short-hex": _variant(BLOB, BLOB[:39]),
    "symbol-target": _variant("mod.rs::*", "mod.rs::run"),
    "line-zero": _variant("lines: 1, 3-4", "lines: 0"),
    "leading-zero": _variant("lines: 1, 3-4", "lines: 01"),
    "empty-span": _variant("lines: 1, 3-4", "lines: 3-3"),
    "reversed-span": _variant("lines: 1, 3-4", "lines: 4-3"),
    "descending": _variant("lines: 1, 3-4", "lines: 5, 2"),
    "overlapping": _variant("lines: 1, 3-4", "lines: 1-4, 3-6"),
    "adjacent": _variant("lines: 1, 3-4", "lines: 1-2, 3"),
    "scope-reason-not-last": _variant(
        " — lines: 1, 3-4 — scope-reason: remove the re-export",
        " — scope-reason: remove the re-export — lines: 1, 3-4",
    ),
    "second-operation": _variant(
        "— operation: delete-lines", "— operation: delete-lines — operation: delete"
    ),
    "en-dash": "- " + CANONICAL.replace(" — ", " – "),
    "missing-path": _variant("src/app/mod.rs", ""),
    "absolute-path": _variant("src/app/mod.rs", "/src/app/mod.rs"),
    "parent-path": _variant("src/app/mod.rs", "src/../app/mod.rs"),
    "dot-path": _variant("src/app/mod.rs", "./src/app/mod.rs"),
    "doubled-slash": _variant("src/app/mod.rs", "src//app/mod.rs"),
    "empty-scope-reason": _variant("remove the re-export", ""),
    "backticked-reason": _variant("remove the re-export", "remove `UNNAMED_PANE`"),
    "reserved-field-in-reason": _variant("remove the re-export", f"drop — base-blob: {BLOB}"),
}


@pytest.mark.parametrize(
    ("line", "outcome"),
    [
        pytest.param(f"- {CANONICAL}", nullcontext(PARSED), id="dash-bullet"),
        pytest.param(f"  * {CANONICAL}", nullcontext(PARSED), id="star-bullet"),
        pytest.param(CANONICAL, nullcontext(PARSED), id="header-form"),
        *(
            pytest.param(line, pytest.raises(DeleteLinesProofError), id=name)
            for name, line in _REJECTED.items()
        ),
    ],
)
def test_parse_delete_lines_proof_grammar(line: str, outcome: AbstractContextManager[Any]) -> None:
    with outcome as expected:
        assert parse_delete_lines_proof(line) == expected


def _proof(data: bytes, *ranges: tuple[int, int], path: str = "src/app/mod.py") -> DeleteLinesProof:
    return DeleteLinesProof(path=path, base_blob=git_blob_id(data), ranges=ranges)


def test_project_delete_lines_binds_base_bytes() -> None:
    assert git_blob_id(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert git_blob_id(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"

    crlf = b"a\r\nb\r\nc\r\n"
    projected = project_delete_lines(_proof(crlf, (2, 2)), crlf)
    assert projected.data == b"a\r\nc\r\n"
    assert projected.blob == git_blob_id(b"a\r\nc\r\n")
    assert (projected.base_count, projected.projected_count) == (3, 2)

    unterminated = b"a\nb\nc"
    assert project_delete_lines(_proof(unterminated, (2, 2)), unterminated).data == b"a\nc"
    assert project_delete_lines(_proof(unterminated, (3, 3)), unterminated).data == b"a\nb\n"
    assert project_delete_lines(_proof(b"a\r\nb", (2, 2)), b"a\r\nb").data == b"a\r\n"

    stale = DeleteLinesProof(
        path="src/app/mod.py", base_blob=git_blob_id(b"old\n"), ranges=((1, 1),)
    )
    with pytest.raises(DeleteLinesProofError, match=git_blob_id(b"new\nline\n")):
        project_delete_lines(stale, b"new\nline\n")
    with pytest.raises(DeleteLinesProofError, match="3 lines"):
        project_delete_lines(_proof(unterminated, (4, 4)), unterminated)
    with pytest.raises(DeleteLinesProofError, match="every line"):
        project_delete_lines(_proof(unterminated, (1, 3)), unterminated)
    with pytest.raises(DeleteLinesProofError, match="carriage return"):
        project_delete_lines(_proof(b"a\rb\n", (1, 1)), b"a\rb\n")
    with pytest.raises(DeleteLinesProofError, match="UTF-8"):
        project_delete_lines(_proof(b"a\n\xff\n", (1, 1)), b"a\n\xff\n")


def test_project_delete_lines_recounts_production_lines() -> None:
    rust = (
        "fn f() {}\n" * 900 + "#[cfg(test)]\nmod tests {\n" + "    fn t() {}\n" * 98 + "}\n"
    ).encode()
    with pytest.raises(DeleteLinesProofError, match="1,000 production lines"):
        project_delete_lines(_proof(rust, (901, 901), path="crates/lib.rs"), rust)
    tail_only = project_delete_lines(_proof(rust, (903, 950), path="crates/lib.rs"), rust)
    assert (tail_only.base_count, tail_only.projected_count) == (900, 900)

    oversized = b"value = 1\n" * 1005
    shrunk = project_delete_lines(_proof(oversized, (1, 15)), oversized)
    assert (shrunk.base_count, shrunk.projected_count) == (1005, 990)
    with pytest.raises(DeleteLinesProofError, match="ceiling"):
        project_delete_lines(_proof(oversized, (1, 5)), oversized)

    assert production_line_count("a\r\nb\r\n", suffix=".py") == 2
    assert production_line_count("a\rb\rc", suffix=".py") == 3
    rust_tail = "fn a() {}\n#[cfg(test)]\nmod t {}\n"
    assert production_line_count(rust_tail, suffix=".rs") == 1
    assert production_line_count(rust_tail, suffix=".py") == 3
