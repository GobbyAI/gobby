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
    separator = "\n\nDiff excerpts:\n"
    manifest = _build_file_manifest(
        files,
        additions=additions,
        deletions=deletions,
        max_chars=max_chars - len(separator),
    )
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


def _build_file_manifest(
    files: list[ChangedFileEvidence],
    *,
    additions: int,
    deletions: int,
    max_chars: int,
) -> str:
    """Render every exact path, aliasing repeated directory prefixes when needed."""
    displays = [item.path for item in files]
    aliases: list[tuple[str, str]] = []

    manifest = _format_file_manifest(files, displays, aliases, additions, deletions)
    if len(manifest) <= max_chars:
        return manifest

    prefix_members: dict[str, list[int]] = {}
    for index, item in enumerate(files):
        components = item.path.split("/")[:-1]
        for depth in range(1, len(components) + 1):
            prefix = "/".join(components[:depth]) + "/"
            prefix_members.setdefault(prefix, []).append(index)

    assigned: set[int] = set()
    candidates = sorted(
        prefix_members.items(),
        key=lambda candidate: len(candidate[0]) * len(candidate[1]),
        reverse=True,
    )
    for prefix, members in candidates:
        eligible = [index for index in members if index not in assigned]
        if len(eligible) < 2:
            continue
        alias = f"@{len(aliases) + 1}/"
        alias_line = f"- {alias} = {prefix}"
        saved_chars = (len(prefix) - len(alias)) * len(eligible) - len(alias_line) - 1
        if saved_chars <= 0:
            continue
        aliases.append((alias, prefix))
        for index in eligible:
            displays[index] = alias + files[index].path.removeprefix(prefix)
            assigned.add(index)
        manifest = _format_file_manifest(files, displays, aliases, additions, deletions)
        if len(manifest) <= max_chars:
            return manifest

    return _collapse_overflow_prefixes(
        files,
        displays,
        aliases,
        additions=additions,
        deletions=deletions,
        max_chars=max_chars,
    )


def _largest_remaining_prefix(
    files: list[ChangedFileEvidence],
    remaining: list[int],
) -> tuple[str, list[int]] | None:
    counts: dict[str, list[int]] = {}
    for index in remaining:
        components = files[index].path.split("/")[:-1]
        for depth in range(1, len(components) + 1):
            prefix = "/".join(components[:depth]) + "/"
            counts.setdefault(prefix, []).append(index)
    candidates = [(prefix, members) for prefix, members in counts.items() if len(members) >= 2]
    if not candidates:
        return None
    prefix, members = max(candidates, key=lambda item: (len(item[1]), len(item[0])))
    return prefix, members


def _collapse_overflow_prefixes(
    files: list[ChangedFileEvidence],
    displays: list[str],
    aliases: list[tuple[str, str]],
    *,
    additions: int,
    deletions: int,
    max_chars: int,
) -> str:
    remaining = list(range(len(files)))
    summaries: list[str] = []
    while True:
        manifest = _format_collapsed_manifest(
            files,
            displays,
            aliases,
            remaining,
            summaries,
            additions,
            deletions,
        )
        if len(manifest) <= max_chars:
            return manifest
        collapse = _largest_remaining_prefix(files, remaining)
        if collapse is None:
            return manifest
        prefix, members = collapse
        member_set = set(members)
        remaining = [index for index in remaining if index not in member_set]
        added = sum(files[index].additions for index in members)
        removed = sum(files[index].deletions for index in members)
        summaries.append(f"- {prefix}** ({len(members)} files, +{added}/-{removed})")


def _format_collapsed_manifest(
    files: list[ChangedFileEvidence],
    displays: list[str],
    aliases: list[tuple[str, str]],
    remaining: list[int],
    summaries: list[str],
    additions: int,
    deletions: int,
) -> str:
    lines = [f"Changed files ({len(files)} total, +{additions}/-{deletions}):"]
    used_aliases = [
        (alias, prefix)
        for alias, prefix in aliases
        if any(displays[index].startswith(alias) for index in remaining)
    ]
    if used_aliases:
        lines.append("Path aliases (exact prefixes):")
        lines.extend(f"- {alias} = {prefix}" for alias, prefix in used_aliases)
    lines.extend(summaries)
    lines.extend(
        f"- {displays[index]} (+{files[index].additions}/-{files[index].deletions})"
        for index in remaining
    )
    return "\n".join(lines)


def _format_file_manifest(
    files: list[ChangedFileEvidence],
    displays: list[str],
    aliases: list[tuple[str, str]],
    additions: int,
    deletions: int,
) -> str:
    lines = [f"Changed files ({len(files)} total, +{additions}/-{deletions}):"]
    if aliases:
        lines.append("Path aliases (exact prefixes):")
        lines.extend(f"- {alias} = {prefix}" for alias, prefix in aliases)
    lines.extend(
        f"- {display} (+{item.additions}/-{item.deletions})"
        for item, display in zip(files, displays, strict=True)
    )
    return "\n".join(lines)


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
