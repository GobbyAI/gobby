"""Production-size primitives and the typed ``delete-lines`` proof for plan validation."""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

PRODUCTION_SIZE_GROWTH_THRESHOLD = 850
PRODUCTION_SIZE_CEILING = 1_000
_PRODUCTION_SUFFIXES = frozenset(
    {".py", ".ts", ".tsx", ".css", ".rs", ".js", ".mjs", ".cjs", ".sh"}
)
_NON_PRODUCTION_PARTS = frozenset({"fixtures", "node_modules", "tests", "vendor"})
_RUST_TEST_MODULE_RE = re.compile(r"^\s*#\[cfg\(test\)\]")
_PROOF_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?`(?P<path>[^`]*)::\*` — operation: delete-lines"
    r" — base-blob: (?P<blob>[0-9a-f]{40})"
    r" — lines: (?P<ranges>[0-9]+(?:-[0-9]+)?(?:, [0-9]+(?:-[0-9]+)?)*)"
    r" — scope-reason: (?P<reason>.*)$"
)
_RESERVED_FIELDS = tuple(
    f" — {key}:" for key in ("operation", "base-blob", "lines", "scope-reason")
)


class DeleteLinesProofError(ValueError):
    """A ``delete-lines`` proof that does not parse or does not hold."""


@dataclass(frozen=True)
class DeleteLinesProof:
    path: str
    base_blob: str
    ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class ProjectedDeletion:
    data: bytes
    blob: str
    base_count: int
    projected_count: int


def is_production_source_path(file_path: str) -> bool:
    """Apply the suffix, directory and test-stem rules for hand-maintained production code."""
    index = file_path.rfind(".")
    if (file_path[index:].lower() if index != -1 else "") not in _PRODUCTION_SUFFIXES:
        return False
    relative = PurePosixPath(file_path)
    if any(part in _NON_PRODUCTION_PARTS for part in relative.parts):
        return False
    stem = relative.stem.lower()
    if stem in {"conftest", "tests"} or stem.startswith("test_"):
        return False
    return not stem.endswith(("_test", "_tests", ".test", ".spec"))


def production_line_count(text: str, *, suffix: str) -> int:
    """Count universal-newline lines; a Rust ``#[cfg(test)]`` tail is not production."""
    count = 0
    for line in io.StringIO(text, newline=None):
        if suffix == ".rs" and _RUST_TEST_MODULE_RE.match(line):
            break
        count += 1
    return count


def git_blob_id(data: bytes) -> str:
    """Return the blob ID ``git hash-object --no-filters`` gives these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def parse_delete_lines_proof(line: str) -> DeleteLinesProof:
    """Parse one Targets entry in the fixed ``delete-lines`` proof grammar."""
    match = _PROOF_RE.match(line)
    if match is None:
        raise DeleteLinesProofError(
            "entry does not match `<path>::*` — operation: delete-lines — base-blob:"
            " <40 hex> — lines: <ranges> — scope-reason: <text>"
        )
    path = match.group("path")
    if not path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise DeleteLinesProofError(f"path {path!r} is not a canonical repository-relative path")
    if any(char.isspace() or char in "\\:" for char in path):
        raise DeleteLinesProofError(f"path {path!r} has whitespace, a backslash or a colon")
    reason = match.group("reason")
    # The separator's space before a reason was consumed by the match, so restore it.
    if not reason.strip() or "`" in reason or any(key in f" {reason}" for key in _RESERVED_FIELDS):
        raise DeleteLinesProofError(
            "scope-reason must be non-empty, with no backtick and no further reserved field"
        )
    ranges: list[tuple[int, int]] = []
    for item in match.group("ranges").split(", "):
        bounds = item.split("-")
        if any(bound.startswith("0") for bound in bounds):
            raise DeleteLinesProofError(f"lines item {item!r} has a zero or a leading zero")
        start, end = int(bounds[0]), int(bounds[-1])
        if len(bounds) == 2 and start >= end:
            raise DeleteLinesProofError(f"lines item {item!r} must be N or A-B with A < B")
        if ranges and start <= ranges[-1][1] + 1:
            raise DeleteLinesProofError(
                f"lines item {item!r} must ascend and keep a line after {ranges[-1][1]}"
            )
        ranges.append((start, end))
    return DeleteLinesProof(path=path, base_blob=match.group("blob"), ranges=tuple(ranges))


def project_delete_lines(proof: DeleteLinesProof, data: bytes) -> ProjectedDeletion:
    """Delete the proof's lines from its exact base bytes and recount production lines."""
    actual = git_blob_id(data)
    if actual != proof.base_blob:
        raise DeleteLinesProofError(
            f"{proof.path} is blob {actual}, not the proof's base-blob {proof.base_blob}"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeleteLinesProofError(f"{proof.path} is not valid UTF-8") from exc
    if "\r" in text.replace("\r\n", ""):
        raise DeleteLinesProofError(f"{proof.path} has a carriage return outside CRLF")
    lines = io.BytesIO(data).readlines()
    if proof.ranges[-1][1] > len(lines):
        raise DeleteLinesProofError(
            f"lines reach {proof.ranges[-1][1]:,} but {proof.path} has {len(lines):,} lines"
        )
    retained = list(lines)
    for start, end in reversed(proof.ranges):
        del retained[start - 1 : end]
    if not retained:
        raise DeleteLinesProofError(f"lines delete every line of {proof.path}")
    projected = b"".join(retained)
    suffix = PurePosixPath(proof.path).suffix
    base_count = production_line_count(text, suffix=suffix)
    projected_count = production_line_count(projected.decode("utf-8"), suffix=suffix)
    if projected_count > base_count:
        raise DeleteLinesProofError(
            f"deleting those lines grows {proof.path} from {base_count:,} to "
            f"{projected_count:,} production lines"
        )
    if projected_count >= PRODUCTION_SIZE_CEILING:
        raise DeleteLinesProofError(
            f"{proof.path} keeps {projected_count:,} production lines, at or above the "
            f"{PRODUCTION_SIZE_CEILING:,}-line ceiling"
        )
    return ProjectedDeletion(projected, git_blob_id(projected), base_count, projected_count)
