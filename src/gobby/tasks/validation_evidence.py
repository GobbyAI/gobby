"""Budgeted diff evidence for task-close criteria review.

Below the budget the evidence is the complete textual diff. Above it, the
truncation is per-file and structured: every changed file keeps its manifest
row and a diff section, omitted spans are declared inline, and lines matching
strings the criteria name (commands, paths, measured numbers) are always
retained so the reviewer never loses the evidence a criterion references.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.MULTILINE)
_BINARY_PATCH_RE = re.compile(r"(?ms)^GIT binary patch\n.*?(?=^diff --git |\Z)")

# Quoted or backticked spans in criteria text: verbatim commands and phrases.
_CRITERIA_SPAN_RE = re.compile(r"`([^`\n]{2,200})`|\"([^\"\n]{2,200})\"|'([^'\n]{2,200})'")
# Path-shaped tokens (contain a slash) or dotted names such as validate.md.
_CRITERIA_PATH_RE = re.compile(r"[\w.~-]+/[\w./~-]*|[\w-]+\.[A-Za-z][\w-]{0,15}")
# Dotted English abbreviations that would otherwise become junk anchors.
_ANCHOR_STOPWORDS = frozenset({"e.g", "i.e", "etc", "vs."})
# snake_case identifiers such as close_task or prompt_chars.
_CRITERIA_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+")
# Numbers, tolerating digit-group separators: 256,000 or 256_000 or 60892.
_CRITERIA_NUMBER_RE = re.compile(r"\d(?:[\d,._]*\d)?")

# At most this many matching lines are recovered per anchor per file, so
# criteria fidelity stays bounded instead of re-inflating the prompt.
_ANCHOR_LINES_PER_ANCHOR = 4
# Recovered lines longer than this are clipped around their content.
_ANCHOR_LINE_MAX_CHARS = 400
# Criteria yield at most this many distinct anchors (they are short texts).
_MAX_ANCHORS = 64
# Reserved for the top-of-evidence truncation notice and section joiners.
_EVIDENCE_NOTICE_RESERVE = 600
# Reserved out of each truncated file's share for its inline drop marker.
_FILE_MARKER_RESERVE = 200


@dataclass(frozen=True, slots=True)
class ChangedFileEvidence:
    """One changed file extracted from a unified Git diff."""

    path: str
    additions: int
    deletions: int
    diff: str
    binary: bool = False


@dataclass(frozen=True, slots=True)
class CloseDiffEvidence:
    """Diff evidence text plus deterministic per-file statistics."""

    text: str
    manifest_count: int
    manifest_chars: int
    excerpt_chars: int
    sha256: str
    truncated: bool = False
    dropped_chars: int = 0


@dataclass(frozen=True, slots=True)
class CriteriaAnchor:
    """One exact string named by the criteria, with its line matcher."""

    text: str
    pattern: re.Pattern[str]


def extract_criteria_anchors(criteria: str) -> tuple[CriteriaAnchor, ...]:
    """Extract exact strings the criteria name: spans, paths, identifiers, numbers."""
    seen: set[str] = set()
    anchors: list[CriteriaAnchor] = []

    def add(text: str, pattern: re.Pattern[str]) -> None:
        if text and text not in seen and len(anchors) < _MAX_ANCHORS:
            seen.add(text)
            anchors.append(CriteriaAnchor(text=text, pattern=pattern))

    for match in _CRITERIA_SPAN_RE.finditer(criteria):
        span = next(group for group in match.groups() if group is not None).strip()
        if len(span) >= 3:
            add(span, re.compile(re.escape(span)))
    for regex in (_CRITERIA_PATH_RE, _CRITERIA_IDENT_RE):
        for match in regex.finditer(criteria):
            token = match.group().rstrip(".")
            if len(token) >= 3 and token not in _ANCHOR_STOPWORDS:
                add(token, re.compile(re.escape(token)))
    for match in _CRITERIA_NUMBER_RE.finditer(criteria):
        digits = re.sub(r"\D", "", match.group())
        if len(digits) < 2:
            continue
        separated = r"[,._]?".join(digits)
        add(digits, re.compile(rf"(?<!\d){separated}(?!\d)"))
    return tuple(anchors)


def build_close_diff_evidence(
    diff: str | None,
    *,
    criteria: str,
    budget_chars: int | None = None,
) -> CloseDiffEvidence:
    """Return diff evidence, complete when it fits and structurally truncated when not.

    With no budget, or when the complete evidence fits it, the text carries the
    full manifest and the complete textual diff (binary payloads excepted).
    Over budget, every changed file still gets its manifest row and a per-file
    diff section; omitted spans are stated inline and lines matching strings
    the criteria name are always retained.
    """
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
    complete_diff = "\n\n".join(item.diff for item in files).rstrip()
    text = f"{manifest}\n\nComplete textual diff:\n{complete_diff or 'none'}"
    if budget_chars is None or len(text) <= budget_chars:
        return CloseDiffEvidence(
            text=text,
            manifest_count=len(files),
            manifest_chars=len(manifest),
            excerpt_chars=len(complete_diff),
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        )

    anchors = extract_criteria_anchors(criteria)
    sections_budget = max(budget_chars - len(manifest) - _EVIDENCE_NOTICE_RESERVE, 0)
    shares = _allocate_shares([len(item.diff) for item in files], sections_budget)
    sections: list[str] = []
    dropped_chars = 0
    for item, share in zip(files, shares, strict=True):
        section, section_dropped_chars = _truncate_file_diff(item, share, anchors)
        sections.append(section)
        dropped_chars += section_dropped_chars
    body = "\n\n".join(sections).rstrip()
    notice = (
        "NOTE: diff evidence was truncated to fit the close-review budget "
        f"({len(body):,} of {len(complete_diff):,} diff characters shown). Every changed "
        "file keeps its complete manifest statistics above and a diff section below; "
        "omitted spans are declared per file, and lines matching strings named by the "
        "criteria are always retained."
    )
    text = f"{manifest}\n\n{notice}\n\nTruncated textual diff:\n{body or 'none'}"
    return CloseDiffEvidence(
        text=text,
        manifest_count=len(files),
        manifest_chars=len(manifest),
        excerpt_chars=len(body),
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        truncated=True,
        dropped_chars=dropped_chars,
    )


def _allocate_shares(sizes: list[int], budget: int) -> list[int]:
    """Waterfill: small files keep everything; the surplus flows to larger files."""
    shares = [0] * len(sizes)
    remaining = budget
    order = sorted(range(len(sizes)), key=lambda index: (sizes[index], index))
    for position, index in enumerate(order):
        fair = remaining // (len(sizes) - position)
        shares[index] = min(sizes[index], fair)
        remaining -= shares[index]
    return shares


def _truncate_file_diff(
    item: ChangedFileEvidence,
    share: int,
    anchors: tuple[CriteriaAnchor, ...],
) -> tuple[str, int]:
    """One file's section: header, head excerpt, drop marker, anchor-matched lines."""
    if len(item.diff) <= share:
        return item.diff, 0

    lines = item.diff.split("\n")
    hunk_starts = [index for index, line in enumerate(lines) if line.startswith("@@")]
    header_end = hunk_starts[0] if hunk_starts else len(lines)

    # The file header block is always shown, even when the share cannot pay
    # for it: representation of every changed file outranks strict fit.
    content_share = max(share - _FILE_MARKER_RESERVE, 0)
    keep_end = header_end
    used = sum(len(lines[index]) + 1 for index in range(header_end))
    for index in range(header_end, len(lines)):
        cost = len(lines[index]) + 1
        if used + cost > content_share:
            break
        used += cost
        keep_end = index + 1
    if keep_end >= len(lines):
        return item.diff, 0

    recovered = _recover_anchor_lines(lines, keep_end, anchors)
    dropped_line_count = len(lines) - keep_end - len(recovered)
    dropped_char_count = sum(
        len(lines[index]) + 1 for index in range(keep_end, len(lines)) if index not in recovered
    )
    parts = lines[:keep_end]
    parts.append(
        f"[... {dropped_line_count} diff lines ({dropped_char_count:,} chars) omitted "
        f"from {item.path} to fit the close-review budget; its manifest statistics "
        "above are complete ...]"
    )
    if recovered:
        parts.append("[criteria-referenced lines retained from the omitted span:]")
        emitted_hunks: set[int] = set()
        for index in sorted(recovered):
            governing = _governing_hunk(hunk_starts, index)
            if governing is not None and governing >= keep_end and governing not in emitted_hunks:
                emitted_hunks.add(governing)
                if governing not in recovered:
                    parts.append(_clip_line(lines[governing]))
            parts.append(_clip_line(lines[index]))
    return "\n".join(parts), dropped_char_count


def _recover_anchor_lines(
    lines: list[str],
    keep_end: int,
    anchors: tuple[CriteriaAnchor, ...],
) -> set[int]:
    """Indices past keep_end whose lines match criteria anchors, capped per anchor."""
    recovered: set[int] = set()
    for anchor in anchors:
        found = 0
        for index in range(keep_end, len(lines)):
            if found >= _ANCHOR_LINES_PER_ANCHOR:
                break
            if anchor.pattern.search(lines[index]):
                recovered.add(index)
                found += 1
    return recovered


def _governing_hunk(hunk_starts: list[int], index: int) -> int | None:
    """The hunk header index governing a line, when one precedes it."""
    governing: int | None = None
    for start in hunk_starts:
        if start > index:
            break
        governing = start
    return governing


def _clip_line(line: str) -> str:
    if len(line) <= _ANCHOR_LINE_MAX_CHARS:
        return line
    return f"{line[:_ANCHOR_LINE_MAX_CHARS]} [... line clipped ...]"


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
    "ChangedFileEvidence",
    "CloseDiffEvidence",
    "CriteriaAnchor",
    "build_close_diff_evidence",
    "extract_criteria_anchors",
]
