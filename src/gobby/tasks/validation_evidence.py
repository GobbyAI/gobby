"""Complete diff evidence for task-close criteria review."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.MULTILINE)
_BINARY_PATCH_RE = re.compile(r"(?ms)^GIT binary patch\n.*?(?=^diff --git |\Z)")


@dataclass(frozen=True, slots=True)
class ChangedFileEvidence:
    """One changed file extracted from a unified Git diff."""

    path: str
    additions: int
    deletions: int
    diff: str
    binary: bool = False


class ValidationEvidenceTooLarge(ValueError):
    """Raised when explicitly bounded evidence cannot fit its caller's limit."""


@dataclass(frozen=True, slots=True)
class CloseDiffEvidence:
    """Complete textual diff plus deterministic per-file statistics."""

    text: str
    manifest_count: int
    manifest_chars: int
    excerpt_chars: int
    sha256: str


def build_close_diff_evidence(
    diff: str | None,
    *,
    criteria: str,
    max_chars: int | None = None,
    max_excerpt_chars: int | None = None,
) -> CloseDiffEvidence:
    """Return complete textual evidence, omitting only encoded binary payloads.

    max_chars remains available for callers that deliberately impose a hard
    bound. Normal close review leaves it unset and applies the configured limit
    to the fully rendered prompt instead. max_excerpt_chars is retained as a
    compatibility keyword and has no effect because textual diffs are complete.
    """
    del criteria, max_excerpt_chars
    by_path: dict[str, ChangedFileEvidence] = {}
    for item in _parse_diff_files(diff or ""):
        previous = by_path.get(item.path)
        if previous is None:
            by_path[item.path] = item
            continue
        by_path[item.path] = ChangedFileEvidence(
            path=item.path,
            additions=previous.additions + item.additions,
            deletions=previous.deletions + item.deletions,
            diff=f"{previous.diff}\n{item.diff}",
            binary=previous.binary or item.binary,
        )

    files = sorted(by_path.values(), key=lambda item: item.path)
    if not files:
        text = "Changed files: none.\nComplete textual diff: none."
        return CloseDiffEvidence(text, 0, len(text), 0, hashlib.sha256(text.encode()).hexdigest())

    additions = sum(item.additions for item in files)
    deletions = sum(item.deletions for item in files)
    manifest = _format_file_manifest(files, additions, deletions)
    diff_text = "\n\n".join(item.diff for item in files).rstrip()
    text = f"{manifest}\n\nComplete textual diff:\n{diff_text or 'none'}"
    if max_chars is not None and len(text) > max_chars:
        raise ValidationEvidenceTooLarge(
            "The complete task-close diff evidence exceeds the caller's explicit bound."
        )
    return CloseDiffEvidence(
        text=text,
        manifest_count=len(files),
        manifest_chars=len(manifest),
        excerpt_chars=len(diff_text),
        sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def _format_file_manifest(
    files: list[ChangedFileEvidence],
    additions: int,
    deletions: int,
) -> str:
    lines = [f"Changed files ({len(files)} total, +{additions}/-{deletions} LOC):"]
    for item in files:
        kind = ", binary payload omitted" if item.binary else ""
        lines.append(f"- {item.path} (+{item.additions}/-{item.deletions} LOC{kind})")
    return "\n".join(lines)


def _parse_diff_files(diff: str) -> list[ChangedFileEvidence]:
    matches = list(_DIFF_HEADER_RE.finditer(diff))
    files: list[ChangedFileEvidence] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index < len(matches) - 1 else len(diff)
        raw_file_diff = diff[start:end].rstrip()
        path = _normalize_diff_path(match.group(2) or match.group(1))
        binary = "GIT binary patch" in raw_file_diff or "Binary files " in raw_file_diff
        file_diff = _strip_binary_payload(raw_file_diff) if binary else raw_file_diff
        additions, deletions = _count_file_stats(file_diff)
        files.append(ChangedFileEvidence(path, additions, deletions, file_diff, binary))
    return files


def _strip_binary_payload(file_diff: str) -> str:
    replacement = "GIT binary patch\n[binary payload omitted; file statistics retained]"
    return _BINARY_PATCH_RE.sub(replacement, file_diff).rstrip()


def _normalize_diff_path(path: str) -> str:
    return path.strip().strip('"')


def _count_file_stats(file_diff: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in file_diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


__all__ = [
    "CloseDiffEvidence",
    "ValidationEvidenceTooLarge",
    "build_close_diff_evidence",
]
