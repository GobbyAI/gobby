"""Structured evidence assembly for task validation prompts."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.MULTILINE)
_HUNK_HEADER_RE = re.compile(r"^@@ .* @@")

_DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
_CONFIG_FILENAMES = {
    ".env",
    ".gitignore",
    "dockerfile",
    "makefile",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "tox.ini",
    "uv.lock",
}
_CONFIG_EXTENSIONS = {".cfg", ".conf", ".ini", ".json", ".toml", ".yaml", ".yml"}
_UI_EXTENSIONS = {".css", ".jsx", ".sass", ".scss", ".svelte", ".tsx", ".vue"}
_SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".kt",
    ".mjs",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".swift",
    ".ts",
}


@dataclass(frozen=True)
class ChangedFileEvidence:
    """One changed file parsed from a git diff."""

    path: str
    additions: int
    deletions: int
    category: str
    diff: str


@dataclass(frozen=True)
class EvidenceOmission:
    """Named evidence omitted or shortened from the validation prompt."""

    path: str
    reason: str


@dataclass(frozen=True)
class ValidationEvidence:
    """Rendered validation evidence and its manifest metadata."""

    text: str
    manifest: tuple[ChangedFileEvidence, ...]
    omissions: tuple[EvidenceOmission, ...]


def build_diff_validation_evidence(
    diff: str | None,
    *,
    max_chars: int,
    max_hunk_lines: int = 60,
    priority_files: Sequence[str] | None = None,
    agent_summary: str | None = None,
    agent_summary_max_chars: int = 2000,
) -> ValidationEvidence:
    """Render a git diff with complete manifest and explicit named omissions."""
    if diff is None:
        return ValidationEvidence(text="", manifest=(), omissions=())
    if not diff:
        return ValidationEvidence(text="", manifest=(), omissions=())

    files = tuple(_parse_diff_files(diff))
    if not files:
        text = _append_agent_summary(
            f"Raw Change Evidence:\n{_shorten_text(diff, max_chars, label='raw change evidence')}",
            agent_summary,
            max_chars=max_chars,
            max_summary_chars=agent_summary_max_chars,
        )
        return ValidationEvidence(text=text, manifest=(), omissions=())

    ordered_files = tuple(sorted(files, key=lambda file: _priority_key(file.path, priority_files)))
    header = _render_manifest(ordered_files)
    full_diff_text = f"{header}\nFull Raw Diff:\n{diff.rstrip()}\n"
    full_text = _append_agent_summary(
        full_diff_text,
        agent_summary,
        max_chars=max_chars,
        max_summary_chars=agent_summary_max_chars,
    )
    if len(full_text) <= max_chars:
        return ValidationEvidence(text=full_text, manifest=ordered_files, omissions=())

    text, omissions = _render_excerpted_diff(
        header,
        ordered_files,
        max_chars=max_chars,
        max_hunk_lines=max_hunk_lines,
    )
    text = _append_agent_summary(
        text,
        agent_summary,
        max_chars=max_chars,
        max_summary_chars=agent_summary_max_chars,
    )
    return ValidationEvidence(text=text, manifest=ordered_files, omissions=tuple(omissions))


def build_summary_validation_evidence(summary: str, *, max_chars: int) -> str:
    """Render non-diff agent prose with an explicit shortening notice."""
    summary = summary.strip()
    if len(summary) <= max_chars:
        return summary
    return _shorten_text(summary, max_chars, label="agent changes summary")


def build_file_context_evidence(file_context: str, *, max_chars: int) -> str:
    """Render file context with an explicit shortening notice."""
    file_context = file_context.strip()
    if len(file_context) <= max_chars:
        return file_context
    return _shorten_text(file_context, max_chars, label="file context")


def categorize_changed_path(path: str) -> str:
    """Return the coarse validation category for a changed path."""
    pure = PurePosixPath(path)
    suffix = pure.suffix.lower()
    name = pure.name.lower()
    parts = {part.lower() for part in pure.parts}

    if "tests" in parts or "test" in parts or name.startswith("test_"):
        return "test"
    if name.endswith("_test.py") or name.endswith(".test.ts") or name.endswith(".test.tsx"):
        return "test"
    if name.endswith(".spec.ts") or name.endswith(".spec.tsx"):
        return "test"
    if "docs" in parts or suffix in _DOC_EXTENSIONS:
        return "docs"
    if name in _CONFIG_FILENAMES or suffix in _CONFIG_EXTENSIONS:
        return "config"
    if suffix in _UI_EXTENSIONS:
        return "ui"
    if suffix in _SOURCE_EXTENSIONS:
        return "source"
    return "other"


def _parse_diff_files(diff: str) -> list[ChangedFileEvidence]:
    matches = list(_DIFF_HEADER_RE.finditer(diff))
    files: list[ChangedFileEvidence] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index < len(matches) - 1 else len(diff)
        file_diff = diff[start:end].rstrip()
        path = _normalize_diff_path(match.group(2) or match.group(1))
        additions, deletions = _count_file_stats(file_diff)
        files.append(
            ChangedFileEvidence(
                path=path,
                additions=additions,
                deletions=deletions,
                category=categorize_changed_path(path),
                diff=file_diff,
            )
        )
    return files


def _normalize_diff_path(path: str) -> str:
    return path.strip().strip('"')


def _count_file_stats(file_diff: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in file_diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def _render_manifest(files: Sequence[ChangedFileEvidence]) -> str:
    total_additions = sum(file.additions for file in files)
    total_deletions = sum(file.deletions for file in files)
    source_ui = [file for file in files if file.category in {"source", "ui"}]
    tests = [file for file in files if file.category == "test"]
    docs_config = [file for file in files if file.category in {"docs", "config"}]
    lines = [
        "Changed File Manifest (authoritative):",
        f"Total files changed: {len(files)} (+{total_additions}/-{total_deletions})",
        f"Source/UI files changed: {_format_category_summary(source_ui)}",
        f"Test files changed: {_format_category_summary(tests)}",
        f"Docs/config files changed: {_format_category_summary(docs_config)}",
        "",
        "Files:",
    ]
    lines.extend(
        f"- {file.path} (+{file.additions}/-{file.deletions}) [{file.category}]" for file in files
    )
    return "\n".join(lines) + "\n"


def _format_category_summary(files: Sequence[ChangedFileEvidence]) -> str:
    if not files:
        return "none"
    return ", ".join(f"{file.path} [{file.category}]" for file in files)


def _render_excerpted_diff(
    header: str,
    files: Sequence[ChangedFileEvidence],
    *,
    max_chars: int,
    max_hunk_lines: int,
) -> tuple[str, list[EvidenceOmission]]:
    omissions: list[EvidenceOmission] = []
    parts = [header, "\nDiff Excerpts:\n"]
    if len("".join(parts)) >= max_chars:
        omissions.extend(
            EvidenceOmission(file.path, "diff details omitted; manifest consumed target budget")
            for file in files
        )
        parts.append(_render_omissions(omissions))
        return "".join(parts), omissions

    for index, file in enumerate(files):
        remaining_files = max(1, len(files) - index)
        current = "".join(parts)
        remaining_budget = max_chars - len(current)
        omission_reserve = 160 * remaining_files
        available = remaining_budget - omission_reserve
        if available < 240:
            omissions.append(
                EvidenceOmission(file.path, "diff details omitted; evidence budget exhausted")
            )
            continue

        excerpt, file_omissions = _excerpt_file_diff(
            file,
            max_chars=available,
            max_hunk_lines=max_hunk_lines,
        )
        omissions.extend(file_omissions)
        parts.append(f"\n### {file.path}\n{excerpt.rstrip()}\n")

    if omissions:
        parts.append(_render_omissions(omissions))
    return "".join(parts), omissions


def _excerpt_file_diff(
    file: ChangedFileEvidence,
    *,
    max_chars: int,
    max_hunk_lines: int,
) -> tuple[str, list[EvidenceOmission]]:
    omissions: list[EvidenceOmission] = []
    lines: list[str] = []
    hunk_header = ""
    hunk_line_count = 0
    skipping_hunk = False
    for line in file.diff.splitlines():
        if _HUNK_HEADER_RE.match(line):
            hunk_header = line
            hunk_line_count = 0
            skipping_hunk = False
            lines.append(line)
            continue
        if hunk_header and (line.startswith("+") or line.startswith("-") or line.startswith(" ")):
            hunk_line_count += 1
            if hunk_line_count > max_hunk_lines:
                if not skipping_hunk:
                    lines.append(f"... [hunk truncated for {file.path}: {hunk_header}] ...")
                    omissions.append(
                        EvidenceOmission(
                            file.path,
                            f"hunk {hunk_header} truncated after {max_hunk_lines} lines",
                        )
                    )
                    skipping_hunk = True
                continue
        lines.append(line)

    excerpt = "\n".join(lines)
    if len(excerpt) <= max_chars:
        return excerpt, omissions

    marker = f"\n... [diff excerpt truncated for {file.path}] ...\n"
    keep_chars = max(0, max_chars - len(marker))
    omissions.append(
        EvidenceOmission(file.path, "diff excerpt shortened to fit validation evidence budget")
    )
    return excerpt[:keep_chars].rstrip() + marker, omissions


def _render_omissions(omissions: Sequence[EvidenceOmission]) -> str:
    lines = ["\nOmitted Evidence:"]
    lines.extend(f"- {omission.path}: {omission.reason}" for omission in omissions)
    return "\n".join(lines) + "\n"


def _append_agent_summary(
    text: str,
    summary: str | None,
    *,
    max_chars: int,
    max_summary_chars: int,
) -> str:
    if not summary:
        return text
    summary = summary.strip()
    if not summary:
        return text
    block = f"\nAgent Changes Summary (supplemental):\n{summary}\n"
    if len(text) + len(block) <= max_chars and len(summary) <= max_summary_chars:
        return text + block

    notice = (
        "\nAgent Changes Summary (supplemental): omitted due to length "
        f"({len(summary)} chars); authoritative diff evidence is above.\n"
    )
    available = max_chars - len(text) - len(notice)
    if available < 240:
        return text + notice

    excerpt = _shorten_text(
        summary,
        min(available, max_summary_chars),
        label="agent changes summary",
    )
    return f"{text}\nAgent Changes Summary (supplemental):\n{excerpt}\n"


def _shorten_text(text: str, max_chars: int, *, label: str) -> str:
    if len(text) <= max_chars:
        return text
    marker = f"\n... [{label} shortened due to length; omitted {len(text) - max_chars} chars] ...\n"
    keep_chars = max(0, max_chars - len(marker))
    return text[:keep_chars].rstrip() + marker


def _priority_key(path: str, priority_files: Sequence[str] | None) -> tuple[int, str]:
    if priority_files and any(
        _path_matches_reference(path, reference) for reference in priority_files
    ):
        return (0, path)
    return (1, path)


def _path_matches_reference(path: str, reference: str) -> bool:
    normalized_path = path.strip("/")
    normalized_reference = reference.strip("/")
    return (
        normalized_path == normalized_reference
        or normalized_path.endswith(f"/{normalized_reference}")
        or normalized_reference.endswith(f"/{normalized_path}")
    )
