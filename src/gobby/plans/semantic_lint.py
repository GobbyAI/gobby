"""Deterministic semantic lint for Plan-Coverage Contract drafts."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path, PurePosixPath
from typing import Any

from gobby.plans.parser import (
    ArtifactKind,
    Kind,
    PlanDocument,
    PlanSection,
    extract_section_dependencies,
)

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
    r"\b(add|create|delete|edit|expose|extract|implement|modify|move|refactor|register|"
    r"remove|rename|replace|split|touch|update|wire)\b",
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
        "area",
        "component",
        "components",
        "file",
        "files",
        "module",
        "modules",
        "notes",
        "owner",
        "scope",
        "status",
        "step",
        "steps",
        "target",
        "targets",
    }
)
_KNOWN_FILE_SUFFIXES = frozenset(
    {
        ".cjs",
        ".css",
        ".go",
        ".html",
        ".java",
        ".js",
        ".json",
        ".jsonl",
        ".jsx",
        ".md",
        ".mjs",
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
PRODUCTION_SIZE_GROWTH_THRESHOLD = 850
PRODUCTION_SIZE_CEILING = 1_000
_PRODUCTION_SUFFIXES = frozenset(
    {".py", ".ts", ".tsx", ".css", ".rs", ".js", ".mjs", ".cjs", ".sh"}
)
_NON_PRODUCTION_PARTS = frozenset({"fixtures", "node_modules", "tests", "vendor"})
_PHASE_ID_RE = re.compile(r"^P\d+$")
_SPLIT_MOVE_RE = re.compile(r"\b(?:split|move)\b", re.IGNORECASE)
_GENERATED_HEADER_RE = re.compile(r"\b(?:auto[- ]?)?generated\b", re.IGNORECASE)
_SCHEMA_TRIGGER_PREFIX = "crates/gcore/assets/schema/migrations/"
_SCHEMA_TRIGGER_PATHS = frozenset(
    {
        "crates/gcore/assets/schema/baseline.sql",
        "crates/gcore/src/schema/assets.rs",
    }
)
_SCHEMA_DERIVED_CARRIERS = frozenset(
    {
        "crates/gcore/assets/schema/catalog.manifest.json",
        "crates/gcore/src/grant/bundle.rs",
        "crates/gcore/tests/schema_contract.rs",
        "crates/gdaemon/tests/cli_contract.rs",
        "src/gobby/storage/schema_expected_identity.json",
    }
)
_CONFIG_DERIVED_CARRIERS = frozenset({"crates/gcore/assets/config/runtime_config_contract.json"})


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


def lint_plan_document(
    plan_doc: PlanDocument,
    *,
    project_root: Path | None = None,
) -> SemanticLintResult:
    """Run deterministic semantic lint against a parsed plan document."""
    issues: list[SemanticLintIssue] = []
    for section in plan_doc.sections:
        if section.kind is not Kind.deliverable:
            continue
        issues.extend(_lint_target_coverage(plan_doc, section))
        table_issue = _lint_table_row_decomposition(plan_doc, section)
        if table_issue is not None:
            issues.append(table_issue)
        if project_root is not None:
            issues.extend(_lint_production_size_growth(plan_doc, section, project_root))
    issues.extend(_lint_shared_target_ordering(plan_doc))
    issues.extend(_lint_derived_carriers(plan_doc))
    return SemanticLintResult(tuple(issues))


def iter_target_block_lines(plan_doc: PlanDocument, section: PlanSection) -> Iterator[str]:
    """Yield header content and continuation lines from each Target/Targets block."""

    body_lines = section_body_lines(plan_doc, section, before_acceptance=True)
    index = 0
    while index < len(body_lines):
        line = body_lines[index]
        match = _TARGET_LINE_RE.match(line)
        if match is None:
            index += 1
            continue

        rest = match.group("rest").strip()
        if rest:
            yield rest

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
                yield candidate
                index += 1
                continue
            break


def collect_target_inventory(plan_doc: PlanDocument, section: PlanSection) -> frozenset[str]:
    """Return normalized file paths listed in a section's Target/Targets inventory."""

    targets: set[str] = set()
    for line in iter_target_block_lines(plan_doc, section):
        targets.update(find_file_paths_in_text(line))
    return frozenset(targets)


def section_body_lines(
    plan_doc: PlanDocument,
    section: PlanSection,
    *,
    before_acceptance: bool,
    skip_fenced: bool = True,
) -> list[str]:
    """Return source lines for a section body, excluding the heading and kind marker."""
    if plan_doc.source_lines:
        lines = list(plan_doc.source_lines)
    else:
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
    candidate = re.sub(r":\d+(?:-\d+)?$", "", candidate)
    if not candidate or "://" in candidate or " " in candidate:
        return None
    if candidate.startswith("#"):
        return None
    if "::" in candidate:
        candidate = candidate.split("::", 1)[0]
    if ":" in candidate and "/" not in candidate:
        return None
    suffix = _suffix(candidate)
    if candidate.rsplit("/", 1)[-1] == suffix:
        # A bare extension such as `.tsx` names a file type, not a file.
        return None
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
                f"refs but missing from Target/Targets: {', '.join(missing)}. "
                "Inventory entries must directly follow the Target/Targets line with no "
                "blank line between them — a blank line ends the block, so bullets after "
                "it are not read as targets. A mentioned path containing '/' must match a "
                "target entry exactly; a bare filename matches any target sharing that "
                "basename. See docs/contracts/plan-coverage.md, 'Target Inventory'."
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


def _lint_shared_target_ordering(plan_doc: PlanDocument) -> list[SemanticLintIssue]:
    deliverables = _deliverables(plan_doc)
    graph = _dependency_graph(plan_doc, deliverables)
    sections_by_file: dict[str, list[str]] = {}
    for section in deliverables:
        for file_path in collect_target_inventory(plan_doc, section):
            sections_by_file.setdefault(file_path, []).append(section.section_id)

    issues: list[SemanticLintIssue] = []
    for file_path, section_ids in sorted(sections_by_file.items()):
        for first, second in combinations(dict.fromkeys(section_ids), 2):
            if _has_dependency_path(graph, first, second) or _has_dependency_path(
                graph, second, first
            ):
                continue
            issues.append(
                SemanticLintIssue(
                    code="shared-target-ordering",
                    section_id=second,
                    message=(
                        f"sections {first} and {second} both target {file_path} but have no "
                        "dependency path between them"
                    ),
                    details={"file_path": file_path, "sections": [first, second]},
                )
            )
    return issues


def _lint_production_size_growth(
    plan_doc: PlanDocument,
    section: PlanSection,
    project_root: Path,
) -> list[SemanticLintIssue]:
    issues: list[SemanticLintIssue] = []
    targets = collect_target_inventory(plan_doc, section)
    for file_path in sorted(targets):
        source_path = project_root / file_path
        if not _is_hand_maintained_production_path(file_path, source_path):
            continue
        line_count = _line_count(source_path)
        if line_count < PRODUCTION_SIZE_GROWTH_THRESHOLD:
            continue
        if _has_new_split_target(plan_doc, section, project_root, file_path):
            continue
        issues.append(
            SemanticLintIssue(
                code="production-size-growth",
                section_id=section.section_id,
                line=section.source_span[0],
                message=(
                    f"target {file_path} has {line_count:,} lines and is already near the "
                    f"{PRODUCTION_SIZE_CEILING:,}-line production ceiling; target a new "
                    "same-extension file and name the split or move in this deliverable"
                ),
                details={
                    "file_path": file_path,
                    "line_count": line_count,
                    "threshold": PRODUCTION_SIZE_GROWTH_THRESHOLD,
                    "ceiling": PRODUCTION_SIZE_CEILING,
                },
            )
        )
    return issues


def _lint_derived_carriers(plan_doc: PlanDocument) -> list[SemanticLintIssue]:
    deliverables = _deliverables(plan_doc)
    graph = _dependency_graph(plan_doc, deliverables)
    targets_by_section = {
        section.section_id: collect_target_inventory(plan_doc, section) for section in deliverables
    }
    issues: list[SemanticLintIssue] = []
    for section in deliverables:
        triggers_by_carriers: dict[frozenset[str], set[str]] = {}
        for target in targets_by_section[section.section_id]:
            required = _required_derived_carriers(target)
            if required:
                triggers_by_carriers.setdefault(required, set()).add(target)
        for required, triggers in triggers_by_carriers.items():
            eligible_sections = {
                candidate.section_id
                for candidate in deliverables
                if candidate.section_id == section.section_id
                or _has_dependency_path(graph, candidate.section_id, section.section_id)
            }
            available = set().union(
                *(targets_by_section[section_id] for section_id in eligible_sections)
            )
            missing = sorted(required - available)
            if not missing:
                continue
            issues.append(
                SemanticLintIssue(
                    code="derived-carriers",
                    section_id=section.section_id,
                    line=section.source_span[0],
                    message=(
                        f"targets {', '.join(sorted(triggers))} require derived carrier "
                        f"Targets in the same or a dependent deliverable: {', '.join(missing)}"
                    ),
                    details={
                        "triggers": sorted(triggers),
                        "missing_carriers": missing,
                    },
                )
            )
    return issues


def _deliverables(plan_doc: PlanDocument) -> list[PlanSection]:
    return [section for section in plan_doc.sections if section.kind is Kind.deliverable]


def _dependency_graph(
    plan_doc: PlanDocument,
    deliverables: list[PlanSection],
) -> dict[str, set[str]]:
    sections_by_id = {section.section_id: section for section in plan_doc.sections}
    deliverable_ids = {section.section_id for section in deliverables}
    phase_members: dict[str, set[str]] = {}
    for section in deliverables:
        phase_id = _section_phase_id(section, sections_by_id)
        if phase_id is not None:
            phase_members.setdefault(phase_id, set()).add(section.section_id)

    graph: dict[str, set[str]] = {section.section_id: set() for section in deliverables}
    for section in deliverables:
        for dependency in extract_section_dependencies(section.title):
            if dependency in deliverable_ids:
                graph[section.section_id].add(dependency)
            elif dependency in phase_members:
                graph[section.section_id].update(phase_members[dependency] - {section.section_id})
    return graph


def _section_phase_id(
    section: PlanSection,
    sections_by_id: dict[str, PlanSection],
) -> str | None:
    current: PlanSection | None = section
    seen: set[str] = set()
    while current is not None and current.section_id not in seen:
        seen.add(current.section_id)
        if _PHASE_ID_RE.fullmatch(current.section_id):
            return current.section_id
        current = sections_by_id.get(current.parent_id) if current.parent_id is not None else None
    return None


def _has_dependency_path(graph: dict[str, set[str]], start: str, target: str) -> bool:
    pending = list(graph.get(start, ()))
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(graph.get(current, ()))
    return False


def _required_derived_carriers(file_path: str) -> frozenset[str]:
    if file_path.startswith(_SCHEMA_TRIGGER_PREFIX) or file_path in _SCHEMA_TRIGGER_PATHS:
        return _SCHEMA_DERIVED_CARRIERS
    if file_path.startswith("src/gobby/config/") and _suffix(file_path) == ".py":
        return _CONFIG_DERIVED_CARRIERS
    return frozenset()


def _is_hand_maintained_production_path(file_path: str, source_path: Path) -> bool:
    relative = PurePosixPath(file_path)
    if _suffix(file_path) not in _PRODUCTION_SUFFIXES:
        return False
    if any(part in _NON_PRODUCTION_PARTS for part in relative.parts):
        return False
    stem = relative.stem.lower()
    if stem in {"conftest", "tests"} or stem.startswith("test_"):
        return False
    if stem.endswith(("_test", "_tests", ".test", ".spec")):
        return False
    return source_path.is_file() and not has_generated_header(source_path)


def has_generated_header(path: Path) -> bool:
    """Return whether a file's opening lines carry a generated marker."""
    try:
        with path.open(encoding="utf-8", errors="ignore") as source:
            header = "".join(next(source, "") for _ in range(5))
    except OSError:
        return False
    return _GENERATED_HEADER_RE.search(header) is not None


def _line_count(path: Path) -> int:
    try:
        with path.open("rb") as source:
            return sum(1 for _ in source)
    except OSError:
        return 0


def _has_new_split_target(
    plan_doc: PlanDocument,
    section: PlanSection,
    project_root: Path,
    large_file: str,
) -> bool:
    extension = _suffix(large_file)
    candidates = {
        target
        for target in _bare_target_paths(plan_doc, section)
        if _suffix(target) == extension and not (project_root / target).exists()
    }
    if not candidates:
        return False
    return any(
        _SPLIT_MOVE_RE.search(line) is not None
        for line in section_body_lines(plan_doc, section, before_acceptance=True)
    )


def _bare_target_paths(plan_doc: PlanDocument, section: PlanSection) -> set[str]:
    paths: set[str] = set()
    for line in iter_target_block_lines(plan_doc, section):
        matches = [match.group(1).strip() for match in _BACKTICK_RE.finditer(line)]
        candidates = matches or [
            token.strip() for token in _BULLET_RE.sub("", line, count=1).split(",") if token.strip()
        ]
        for candidate in candidates:
            if "::" in candidate:
                continue
            normalized = normalize_file_path(candidate)
            if normalized is not None:
                paths.add(normalized)
    return paths


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
