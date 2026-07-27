"""Bounded diff shaping for the task-close criteria review."""

from __future__ import annotations

import re
from dataclasses import dataclass

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.MULTILINE)
_BINARY_FILES_RE: re.Pattern[str] = re.compile(
    r"^Binary files (?P<left>\S+) and (?P<right>\S+) differ$",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class ChangedFileEvidence:
    """One changed file extracted from a unified Git diff."""

    path: str
    additions: int
    deletions: int
    diff: str


class ValidationEvidenceTooLarge(ValueError):
    """Raised when a complete file manifest cannot fit the bounded prompt."""


@dataclass(frozen=True, slots=True)
class CloseDiffEvidence:
    """Bounded criteria-review input with a complete file manifest."""

    text: str
    manifest_count: int
    manifest_chars: int
    excerpt_chars: int


def build_close_diff_evidence(
    diff: str | None,
    *,
    criteria: str,
    max_chars: int = 5_500,
    max_excerpt_chars: int = 4_000,
) -> CloseDiffEvidence:
    """Shape the linked diff for the bounded task-close criteria review."""
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
        )
    files = list(by_path.values())
    if not files:
        text = "Changed files: none.\nDiff excerpts: none."
        return CloseDiffEvidence(text, 0, len(text), 0)

    additions = sum(item.additions for item in files)
    deletions = sum(item.deletions for item in files)
    manifest = "\n".join(
        [
            f"Changed files ({len(files)} total, +{additions}/-{deletions}):",
            *(f"- {item.path} (+{item.additions}/-{item.deletions})" for item in files),
        ]
    )
    separator = "\n\nDiff excerpts:\n"
    if len(manifest) + len(separator) > max_chars:
        raise ValidationEvidenceTooLarge(
            "The complete changed-file manifest exceeds the bounded criteria-review prompt. "
            "Split the task into a smaller commit set before closing."
        )

    criteria_folded = criteria.casefold()
    ordered = sorted(
        files,
        key=lambda item: (
            0
            if item.path.casefold() in criteria_folded
            or item.path.rsplit("/", 1)[-1].casefold() in criteria_folded
            else 1,
            item.path,
        ),
    )
    excerpt_budget = min(max_excerpt_chars, max_chars - len(manifest) - len(separator))
    excerpt_parts: list[str] = []
    excerpt_chars = 0
    for item in ordered:
        prefix = f"\n### {item.path}\n"
        remaining = excerpt_budget - excerpt_chars
        if remaining <= len(prefix):
            break
        part = prefix + item.diff[: remaining - len(prefix)]
        excerpt_parts.append(part)
        excerpt_chars += len(part)

    excerpts = "".join(excerpt_parts).rstrip()
    text = f"{manifest}{separator}{excerpts or 'none'}"
    return CloseDiffEvidence(
        text=text,
        manifest_count=len(files),
        manifest_chars=len(manifest),
        excerpt_chars=len(excerpts),
    )


def _parse_diff_files(diff: str) -> list[ChangedFileEvidence]:
    matches = list(_DIFF_HEADER_RE.finditer(diff))
    files: list[ChangedFileEvidence] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index < len(matches) - 1 else len(diff)
        file_diff = diff[start:end].rstrip()
        path = _normalize_diff_path(match.group(2) or match.group(1))
        additions, deletions = _count_file_stats(file_diff)
        files.append(ChangedFileEvidence(path, additions, deletions, file_diff))

    known_paths = {item.path for item in files}
    for match in _BINARY_FILES_RE.finditer(diff):
        path = _normalize_binary_diff_path(match.group("left"), match.group("right"))
        if path in known_paths:
            continue
        files.append(ChangedFileEvidence(path, 0, 0, match.group(0)))
        known_paths.add(path)
    return files


def _normalize_diff_path(path: str) -> str:
    return path.strip().strip('"')


def _normalize_binary_diff_path(left: str, right: str) -> str:
    candidate = right if right != "/dev/null" else left
    return _normalize_diff_path(candidate.removeprefix("b/").removeprefix("a/"))


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
