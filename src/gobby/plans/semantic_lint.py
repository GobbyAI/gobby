"""Deterministic semantic lint for Plan-Coverage Contract drafts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from gobby.plans.parser import ArtifactKind, Kind, PlanDocument, PlanSection

_TARGET_LINE_RE = re.compile(r"^\s*Targets?\s*:\s*(?P<rest>.*)$", re.IGNORECASE)
_ACCEPTANCE_RE = re.compile(r"^\s*\*\*Acceptance:\*\*\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_PATH_TOKEN_RE = re.compile(
    r"(?<![\w.-])"
    r"(?P<path>(?:\.?[A-Za-z0-9_-]+/)+[A-Za-z0-9_.@+-]+"
    r"|[A-Za-z0-9_.@+-]+\.(?:py|md|yaml|yml|toml|json|jsonl|txt|ts|tsx|js|jsx|css|html|sql|sh|rs|go))"
    r"(?![\w.-])"
)
_ARTIFACT_REF_RE = re.compile(
    r"\b(?:file|behavior)\s*:\s*(?:`(?P<ticked>[^`]+)`|(?P<bare>[^\s,.;)]+))",
    re.IGNORECASE,
)
_BODY_PATH_INTENT_RE = re.compile(
    r"\b(add|create|delete|edit|extract|implement|modify|move|refactor|remove|rename|replace|split|touch|update)\b",
    re.IGNORECASE,
)
_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
_WORK_TABLE_HEADERS = frozenset(
    {
        "work",
        "task",
        "tasks",
        "item",
        "items",
        "change",
        "changes",
        "deliverable",
        "deliverables",
        "implementation",
    }
)
_KNOWN_FILE_SUFFIXES = frozenset(
    {
        ".css",
        ".go",
        ".html",
        ".java",
        ".js",
        ".json",
        ".jsonl",
        ".jsx",
        ".md",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
)


@dataclass(frozen=True)
class SemanticLintIssue:
    """One deterministic plan-lint violation."""

    code: str
    section_id: str
    message: str
    line: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_error(self) -> str:
        location = f"line {self.line}: " if self.line is not None else ""
        return f"{self.code}: section {self.section_id}: {location}{self.message}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "section_id": self.section_id,
            "message": self.message,
        }
        if self.line is not None:
            payload["line"] = self.line
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class SemanticLintResult:
    """Semantic lint result with stable error text for existing callers."""

    issues: tuple[SemanticLintIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.issues

    @property
    def errors(self) -> list[str]:
        return [issue.to_error() for issue in self.issues]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def lint_plan_document(plan_doc: PlanDocument) -> SemanticLintResult:
    """Run pure Markdown semantic lint against a parsed plan document."""
    issues: list[SemanticLintIssue] = []
    for section in plan_doc.sections:
        if section.kind is not Kind.deliverable:
            continue
        issues.extend(_lint_target_coverage(plan_doc, section))
        table_issue = _lint_table_row_decomposition(plan_doc, section)
        if table_issue is not None:
            issues.append(table_issue)
    return SemanticLintResult(tuple(issues))


def collect_target_inventory(plan_doc: PlanDocument, section: PlanSection) -> frozenset[str]:
    """Return normalized file paths listed in a section's Target/Targets inventory."""
    body_lines = section_body_lines(plan_doc, section, before_acceptance=True)
    targets: set[str] = set()
    index = 0
    while index < len(body_lines):
        line = body_lines[index]
        match = _TARGET_LINE_RE.match(line)
        if match is None:
            index += 1
            continue

        rest = match.group("rest").strip()
        if rest:
            targets.update(find_file_paths_in_text(rest))
            index += 1
            continue

        index += 1
        while index < len(body_lines):
            candidate = body_lines[index]
            stripped = candidate.strip()
            if not stripped:
                break
            if _TARGET_LINE_RE.match(candidate) or _ACCEPTANCE_RE.match(candidate):
                break
            if stripped.startswith("#") or stripped.startswith("`kind:"):
                break
            if _BULLET_RE.match(candidate) or "`" in candidate or "/" in candidate:
                targets.update(find_file_paths_in_text(candidate))
                index += 1
                continue
            break
    return frozenset(targets)


def section_body_lines(
    plan_doc: PlanDocument,
    section: PlanSection,
    *,
    before_acceptance: bool,
    skip_fenced: bool = True,
) -> list[str]:
    """Return source lines for a section body, excluding the heading and kind marker."""
    lines = plan_doc.source_path.read_text(encoding="utf-8").splitlines()
    start_line, end_line = section.source_span
    raw_lines = lines[start_line:end_line]
    body_lines: list[str] = []
    in_fence = False
    for line in raw_lines:
        stripped = line.strip()
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            if skip_fenced:
                continue
        if skip_fenced and in_fence:
            continue
        if stripped.startswith("`kind:") and stripped.endswith("`"):
            continue
        if before_acceptance and _ACCEPTANCE_RE.match(stripped):
            break
        body_lines.append(line)
    return body_lines


def find_file_paths_in_text(text: str) -> set[str]:
    """Extract normalized concrete file paths from Markdown/prose text."""
    found: set[str] = set()
    for match in _BACKTICK_RE.finditer(text):
        normalized = normalize_file_path(match.group(1))
        if normalized is not None:
            found.add(normalized)

    for match in _PATH_TOKEN_RE.finditer(text):
        normalized = normalize_file_path(match.group("path"))
        if normalized is not None:
            found.add(normalized)
    return found


def normalize_file_path(value: str) -> str | None:
    """Normalize a prose artifact ref to a comparable file path."""
    candidate = value.strip().strip("`'").strip('"')
    candidate = candidate.rstrip(".,;:)")
    if not candidate or "://" in candidate or " " in candidate:
        return None
    if candidate.startswith("#"):
        return None
    if "::" in candidate:
        candidate = candidate.split("::", 1)[0]
    if ":" in candidate and "/" not in candidate:
        return None
    suffix = _suffix(candidate)
    if "/" in candidate:
        if candidate.endswith("/") or suffix not in _KNOWN_FILE_SUFFIXES:
            return None
        return candidate
    if suffix in _KNOWN_FILE_SUFFIXES:
        return candidate
    return None


def _lint_target_coverage(plan_doc: PlanDocument, section: PlanSection) -> list[SemanticLintIssue]:
    targets = collect_target_inventory(plan_doc, section)
    mentioned = _mentioned_paths(plan_doc, section)
    missing = sorted(path for path in mentioned if not _path_covered_by_targets(path, targets))
    if not missing:
        return []
    return [
        SemanticLintIssue(
            code="target-coverage",
            section_id=section.section_id,
            line=section.source_span[0],
            message=(
                "concrete file paths are mentioned in the deliverable body or acceptance "
                f"refs but missing from Target/Targets: {', '.join(missing)}"
            ),
            details={
                "missing_paths": missing,
                "targets": sorted(targets),
            },
        )
    ]


def _mentioned_paths(plan_doc: PlanDocument, section: PlanSection) -> set[str]:
    paths: set[str] = set()
    for line in section_body_lines(plan_doc, section, before_acceptance=True):
        if _TARGET_LINE_RE.match(line):
            continue
        paths.update(_find_change_intent_file_paths(line))

    for item in section.acceptance_items:
        if item.artifact_kind in {ArtifactKind.file, ArtifactKind.behavior}:
            normalized = normalize_file_path(item.artifact_ref)
            if normalized is not None:
                paths.add(normalized)
        for match in _ARTIFACT_REF_RE.finditer(item.prose):
            value = match.group("ticked") or match.group("bare") or ""
            normalized = normalize_file_path(value)
            if normalized is not None:
                paths.add(normalized)
    return paths


def _find_change_intent_file_paths(text: str) -> set[str]:
    found: set[str] = set()
    for match in _BACKTICK_RE.finditer(text):
        if not _BODY_PATH_INTENT_RE.search(text[: match.start()]):
            continue
        normalized = normalize_file_path(match.group(1))
        if normalized is not None:
            found.add(normalized)
    for match in _PATH_TOKEN_RE.finditer(text):
        if not _BODY_PATH_INTENT_RE.search(text[: match.start()]):
            continue
        normalized = normalize_file_path(match.group("path"))
        if normalized is not None:
            found.add(normalized)
    return found


def _path_covered_by_targets(path: str, targets: frozenset[str]) -> bool:
    if path in targets:
        return True
    if "/" in path:
        return False
    return any(target.rsplit("/", 1)[-1] == path for target in targets)


def _lint_table_row_decomposition(
    plan_doc: PlanDocument, section: PlanSection
) -> SemanticLintIssue | None:
    rows = _work_table_data_row_count(section_body_lines(plan_doc, section, before_acceptance=True))
    acceptance_count = len(section.acceptance_items)
    if rows <= acceptance_count:
        return None
    missing = rows - acceptance_count
    return SemanticLintIssue(
        code="table-row-decomposition",
        section_id=section.section_id,
        line=section.source_span[0],
        message=(
            "work-item table data-row count exceeds acceptance-item count "
            f"({rows} rows, {acceptance_count} acceptance items, missing rows: {missing})"
        ),
        details={
            "table_data_rows": rows,
            "acceptance_items": acceptance_count,
            "missing_rows": missing,
        },
    )


def _work_table_data_row_count(lines: list[str]) -> int:
    total = 0
    index = 0
    while index + 1 < len(lines):
        header = lines[index]
        separator = lines[index + 1]
        if "|" not in header or not _is_table_separator(separator):
            index += 1
            continue
        data_index = index + 2
        data_rows = 0
        while data_index < len(lines) and "|" in lines[data_index] and lines[data_index].strip():
            data_rows += 1
            data_index += 1
        if data_rows and _is_work_table(header):
            total += data_rows
        index = max(data_index, index + 1)
    return total


def _is_work_table(header: str) -> bool:
    cells = [cell.strip().lower() for cell in header.strip().strip("|").split("|")]
    return any(cell in _WORK_TABLE_HEADERS for cell in cells)


def _is_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells)


def _suffix(path: str) -> str:
    index = path.rfind(".")
    if index == -1:
        return ""
    return path[index:].lower()
