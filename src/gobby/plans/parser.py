"""Markdown parser for Gobby implementation plans."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml

from gobby.storage.tasks._models import TDD_ELIGIBLE_CATEGORIES

ParseMode = Literal["draft", "expansion", "strict"]

PLAN_HEADING_REGEX: re.Pattern[str] = re.compile(
    r"^#{2,6}\s+(?:§\s*)?(?P<section_id>"
    r"(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?)"
    r")(?=\s|[).:-]|$)"
)

_HEADING_LINE_RE = re.compile(r"^(?P<marks>#{2,6})\s+")
_KIND_LINE_RE = re.compile(r"^`?kind:\s*(?P<kind>[a-z_]+)`?$")
_PLAN_ID_RE = re.compile(r"^\s*>?\s*\*\*Plan ID:\*\*\s*(?P<plan_id>.+?)\s*$")
_SECTION_DEPENDS_RE = re.compile(r"\(depends:\s*(?P<depends>[^)]+)\)", flags=re.IGNORECASE)
_ACCEPTANCE_MARKER = "**Acceptance:**"
_ACCEPTANCE_BULLET_RE = re.compile(
    r"^\s*-\s+(?P<item_id>[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)+)"
    r"\s+(?:-|\u2014)\s+(?P<prose>.*)$"
)
_ARTIFACT_RE = re.compile(
    r"\b(?P<kind>file|symbol|test|behavior):\s*"
    r"""(?P<ref>`[^`]+`|"[^"]+"|'[^']+'|.*?)(?=\s+\b(?:file|symbol|test|behavior):|$)"""
)
_FENCE_OPENER_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
MISSING_PLAN_ID_SENTINEL = "unknown"


def resolve_plan_id(plan_id: str | None) -> str:
    """Return the plan ID used for generated and validated covers labels."""
    return plan_id or MISSING_PLAN_ID_SENTINEL


def extract_section_dependencies(title: str) -> tuple[str, ...]:
    """Extract dependency section IDs from a ``(depends: X, Y)`` annotation."""
    match = _SECTION_DEPENDS_RE.search(title)
    if match is None:
        return ()
    return tuple(part.strip() for part in match.group("depends").split(",") if part.strip())


def strip_section_dependencies(title: str) -> str:
    """Remove a ``(depends: ...)`` annotation from a section title."""
    return _SECTION_DEPENDS_RE.sub("", title)


class Kind(StrEnum):
    deliverable = "deliverable"
    framing = "framing"
    verification = "verification"
    deferred = "deferred"
    manifest = "manifest"


class ArtifactKind(StrEnum):
    file = "file"
    symbol = "symbol"
    test = "test"
    behavior = "behavior"


@dataclass(frozen=True)
class AcceptanceItem:
    item_id: str
    prose: str
    artifact_kind: ArtifactKind
    artifact_ref: str
    source_line: int


@dataclass(frozen=True)
class Deferral:
    task_ref: str
    reason: str
    owner: str
    original_acceptance_items: tuple[AcceptanceItem, ...]
    raw_block: str


@dataclass(frozen=True)
class PlanSection:
    section_id: str
    parent_id: str | None
    heading_level: int
    title: str
    kind: Kind
    acceptance_items: tuple[AcceptanceItem, ...]
    deferral: Deferral | None
    source_span: tuple[int, int]


@dataclass(frozen=True)
class ManifestEntry:
    title: str
    category: str
    task_type: str
    depends_on: tuple[str, ...]
    validation_criteria: str
    labels: tuple[str, ...]
    assigned_agent: str
    tdd: bool
    source_section: str
    source_line: int


@dataclass(frozen=True)
class PlanDocument:
    plan_id: str | None
    source_path: Path
    source_hash: str
    sections: tuple[PlanSection, ...]
    framing_headings: tuple[tuple[int, str, int], ...]
    source_lines: tuple[str, ...] = ()
    manifest_entries: tuple[ManifestEntry, ...] = ()


class PlanParseError(ValueError):
    """Raised on any structural violation."""

    def __init__(self, errors: list[tuple[int, str]], source_path: Path) -> None:
        self.errors = errors
        self.source_path = source_path
        details = "; ".join(f"line {line}: {message}" for line, message in errors)
        super().__init__(f"{source_path}: {details}")


class PlanKind(StrEnum):
    implementation = "implementation"
    strategy = "strategy"


@dataclass(frozen=True)
class _Heading:
    line_index: int
    raw: str
    level: int
    section_id: str | None
    title: str


@dataclass(frozen=True)
class _Fence:
    char: str
    length: int


def parse_plan(
    path: Path,
    *,
    plan_kind: PlanKind = PlanKind.implementation,
    parse_mode: ParseMode = "strict",
) -> PlanDocument:
    """Parse a markdown plan into a structured AST."""

    source_bytes = path.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    lines = source_bytes.decode("utf-8").splitlines()
    mask = _compute_fence_mask(lines)
    headings = _collect_headings(lines, mask)
    plan_id = _parse_plan_id(lines, mask)

    errors: list[tuple[int, str]] = []
    sections: list[PlanSection] = []
    framing_headings: list[tuple[int, str, int]] = []
    seen_section_ids: set[str] = set()
    section_stack: list[PlanSection] = []

    for index, heading in enumerate(headings):
        if index + 1 < len(headings):
            end_index = headings[index + 1].line_index - 1
        else:
            end_index = len(lines) - 1
        kind = _section_kind(lines, mask, heading.line_index + 1, end_index)

        if heading.section_id is None:
            _handle_noncanonical_heading(
                heading=heading,
                kind=kind,
                plan_kind=plan_kind,
                framing_headings=framing_headings,
                errors=errors,
            )
            continue

        if kind is None:
            if plan_kind is PlanKind.strategy:
                _append_framing_section(
                    heading=heading,
                    end_index=end_index,
                    sections=sections,
                    section_stack=section_stack,
                    seen_section_ids=seen_section_ids,
                    errors=errors,
                )
                continue
            errors.append((heading.line_index + 1, "missing kind: front-matter"))
            continue

        if heading.section_id in seen_section_ids:
            errors.append((heading.line_index + 1, f"duplicate section ID {heading.section_id!r}"))
            continue

        seen_section_ids.add(heading.section_id)
        acceptance_items: tuple[AcceptanceItem, ...] = ()
        deferral: Deferral | None = None

        if kind is Kind.deliverable:
            acceptance_items = _parse_acceptance_items(
                lines=lines,
                mask=mask,
                start_index=heading.line_index + 1,
                end_index=end_index,
                section_id=heading.section_id,
                errors=errors,
            )
        elif kind is Kind.deferred:
            deferral = _parse_deferral(
                lines=lines,
                start_index=heading.line_index + 1,
                end_index=end_index,
                source_path=path,
                errors=errors,
            )

        section = PlanSection(
            section_id=heading.section_id,
            parent_id=_parent_for_heading(section_stack, heading.level),
            heading_level=heading.level,
            title=heading.title,
            kind=kind,
            acceptance_items=acceptance_items,
            deferral=deferral,
            source_span=(heading.line_index + 1, max(heading.line_index, end_index) + 1),
        )
        sections.append(section)
        _push_section(section_stack, section)

    manifest_entries = _resolve_manifest(
        lines=lines,
        sections=sections,
        plan_id=plan_id,
        plan_kind=plan_kind,
        parse_mode=parse_mode,
        errors=errors,
    )

    if errors:
        raise PlanParseError(errors, path)

    return PlanDocument(
        plan_id=plan_id,
        source_path=path,
        source_hash=source_hash,
        sections=tuple(sections),
        framing_headings=tuple(framing_headings),
        source_lines=tuple(lines),
        manifest_entries=manifest_entries,
    )


def _compute_fence_mask(lines: list[str]) -> list[bool]:
    mask = [False] * len(lines)
    open_fence: _Fence | None = None

    for index, line in enumerate(lines):
        if open_fence is None:
            opener = _match_fence_opener(line)
            if opener is None:
                continue
            mask[index] = True
            open_fence = opener
            continue

        mask[index] = True
        if _is_fence_closer(line, open_fence):
            open_fence = None

    return mask


def _match_fence_opener(line: str) -> _Fence | None:
    match = _FENCE_OPENER_RE.match(line)
    if match is None:
        return None
    fence = match.group("fence")
    return _Fence(char=fence[0], length=len(fence))


def _is_fence_closer(line: str, open_fence: _Fence) -> bool:
    if line.startswith("    "):
        return False
    stripped = line.lstrip(" ")
    indent = len(line) - len(stripped)
    if indent > 3:
        return False
    delimiter = open_fence.char * open_fence.length
    if not stripped.startswith(delimiter):
        return False
    delimiter_length = 0
    for char in stripped:
        if char != open_fence.char:
            break
        delimiter_length += 1
    if delimiter_length < open_fence.length:
        return False
    return stripped[delimiter_length:].strip() == ""


def _collect_headings(lines: list[str], mask: list[bool]) -> list[_Heading]:
    headings: list[_Heading] = []
    for index, line in enumerate(lines):
        if mask[index]:
            continue
        heading_match = _HEADING_LINE_RE.match(line)
        if heading_match is None:
            continue
        canonical_match = PLAN_HEADING_REGEX.match(line)
        section_id = canonical_match.group("section_id") if canonical_match else None
        title = _heading_title(line, canonical_match)
        headings.append(
            _Heading(
                line_index=index,
                raw=line,
                level=len(heading_match.group("marks")),
                section_id=section_id,
                title=title,
            )
        )
    return headings


def _heading_title(line: str, canonical_match: re.Match[str] | None) -> str:
    if canonical_match is None:
        return line.lstrip("#").strip()
    suffix = line[canonical_match.end() :].strip()
    if suffix[:1] in {")", ".", ":", "-"}:
        suffix = suffix[1:].strip()
    return suffix


def _parse_plan_id(lines: list[str], mask: list[bool]) -> str | None:
    for index, line in enumerate(lines):
        if index < len(mask) and mask[index]:
            continue
        match = _PLAN_ID_RE.match(line)
        if match is None:
            continue
        return _clean_ref(match.group("plan_id"))
    return None


def _section_kind(
    lines: list[str], mask: list[bool], start_index: int, end_index: int
) -> Kind | None:
    for index in range(start_index, end_index + 1):
        if index >= len(lines) or mask[index]:
            continue
        stripped = lines[index].strip()
        if not stripped:
            continue
        match = _KIND_LINE_RE.match(stripped)
        if match is None:
            return None
        try:
            return Kind(match.group("kind"))
        except ValueError:
            return None
    return None


def _handle_noncanonical_heading(
    *,
    heading: _Heading,
    kind: Kind | None,
    plan_kind: PlanKind,
    framing_headings: list[tuple[int, str, int]],
    errors: list[tuple[int, str]],
) -> None:
    if kind is Kind.framing or (kind is None and plan_kind is PlanKind.strategy):
        framing_headings.append((heading.line_index + 1, heading.raw, heading.level))
        return
    errors.append((heading.line_index + 1, "non-canonical heading missing kind: framing"))


def _append_framing_section(
    *,
    heading: _Heading,
    end_index: int,
    sections: list[PlanSection],
    section_stack: list[PlanSection],
    seen_section_ids: set[str],
    errors: list[tuple[int, str]],
) -> None:
    if heading.section_id is None:
        return
    if heading.section_id in seen_section_ids:
        errors.append((heading.line_index + 1, f"duplicate section ID {heading.section_id!r}"))
        return
    seen_section_ids.add(heading.section_id)
    section = PlanSection(
        section_id=heading.section_id,
        parent_id=_parent_for_heading(section_stack, heading.level),
        heading_level=heading.level,
        title=heading.title,
        kind=Kind.framing,
        acceptance_items=(),
        deferral=None,
        source_span=(heading.line_index + 1, max(heading.line_index, end_index) + 1),
    )
    sections.append(section)
    _push_section(section_stack, section)


def _parse_acceptance_items(
    *,
    lines: list[str],
    mask: list[bool],
    start_index: int,
    end_index: int,
    section_id: str,
    errors: list[tuple[int, str]],
) -> tuple[AcceptanceItem, ...]:
    marker_index = _find_acceptance_marker(lines, mask, start_index, end_index)
    if marker_index is None:
        errors.append(
            (start_index, f"deliverable section {section_id!r} missing **Acceptance:** block")
        )
        return ()

    items: list[AcceptanceItem] = []
    current_id: str | None = None
    current_parts: list[str] = []
    current_line = 0

    def flush_current() -> None:
        nonlocal current_id, current_parts, current_line
        if current_id is None:
            return
        prose = " ".join(part.strip() for part in current_parts if part.strip()).strip()
        item = _build_acceptance_item(current_id, prose, current_line, section_id, errors)
        if item is not None:
            items.append(item)
        current_id = None
        current_parts = []
        current_line = 0

    for index in range(marker_index + 1, end_index + 1):
        if index >= len(lines) or mask[index]:
            continue
        line = lines[index]
        bullet_match = _ACCEPTANCE_BULLET_RE.match(line)
        if bullet_match is not None:
            flush_current()
            current_id = bullet_match.group("item_id")
            current_parts = [bullet_match.group("prose")]
            current_line = index + 1
            continue
        if current_id is not None:
            current_parts.append(line)

    flush_current()

    if not items:
        errors.append(
            (marker_index + 1, f"deliverable section {section_id!r} has no acceptance items")
        )
    return tuple(items)


def _find_acceptance_marker(
    lines: list[str], mask: list[bool], start_index: int, end_index: int
) -> int | None:
    for index in range(start_index, end_index + 1):
        if index < len(lines) and not mask[index] and lines[index].strip() == _ACCEPTANCE_MARKER:
            return index
    return None


def _build_acceptance_item(
    item_id: str,
    prose: str,
    source_line: int,
    section_id: str,
    errors: list[tuple[int, str]],
) -> AcceptanceItem | None:
    if not item_id.startswith(f"{section_id}."):
        errors.append(
            (source_line, f"acceptance item {item_id!r} does not belong to section {section_id!r}")
        )
        return None

    artifact = _first_artifact(prose)
    if artifact is None:
        errors.append((source_line, f"acceptance item {item_id!r} has no artifact reference"))
        return None

    artifact_kind, artifact_ref = artifact
    return AcceptanceItem(
        item_id=item_id,
        prose=prose,
        artifact_kind=artifact_kind,
        artifact_ref=artifact_ref,
        source_line=source_line,
    )


def _first_artifact(prose: str) -> tuple[ArtifactKind, str] | None:
    for match in _ARTIFACT_RE.finditer(prose):
        artifact_ref = _clean_ref(match.group("ref"))
        if not artifact_ref:
            continue
        return ArtifactKind(match.group("kind")), artifact_ref
    return None


def _parse_deferral(
    *,
    lines: list[str],
    start_index: int,
    end_index: int,
    source_path: Path,
    errors: list[tuple[int, str]],
) -> Deferral | None:
    block = _find_yaml_fence(lines, start_index, end_index)
    if block is None:
        errors.append((start_index, "deferred section missing YAML deferral object"))
        return None

    block_start, raw_block = block
    try:
        data = yaml.safe_load(raw_block)
    except yaml.YAMLError as exc:
        errors.append((block_start, f"invalid deferred YAML: {exc}"))
        return None

    if not isinstance(data, dict):
        errors.append((block_start, "deferred YAML must be a mapping"))
        return None

    required_fields = ("task_ref", "reason", "owner", "original_acceptance_items")
    missing = [field for field in required_fields if not data.get(field)]
    if missing:
        errors.append((block_start, f"deferred YAML missing fields: {', '.join(missing)}"))
        return None

    original_items = _parse_original_acceptance_items(
        data["original_acceptance_items"], block_start, source_path, errors
    )
    if original_items is None:
        return None

    return Deferral(
        task_ref=str(data["task_ref"]),
        reason=str(data["reason"]),
        owner=str(data["owner"]),
        original_acceptance_items=original_items,
        raw_block=raw_block,
    )


def _find_yaml_fence(lines: list[str], start_index: int, end_index: int) -> tuple[int, str] | None:
    index = start_index
    while index <= end_index and index < len(lines):
        opener = _match_fence_opener(lines[index])
        if opener is None:
            index += 1
            continue
        info = _fence_info(lines[index])
        close_index = _find_fence_close(lines, index + 1, end_index, opener)
        if info in {"yaml", "yml"} and close_index is not None:
            raw_block = "\n".join(lines[index + 1 : close_index])
            return index + 1, raw_block
        index = close_index + 1 if close_index is not None else end_index + 1
    return None


def _fence_info(line: str) -> str:
    match = _FENCE_OPENER_RE.match(line)
    if match is None:
        return ""
    return match.group("info").strip().split(maxsplit=1)[0].lower()


def _find_fence_close(
    lines: list[str], start_index: int, end_index: int, opener: _Fence
) -> int | None:
    for index in range(start_index, end_index + 1):
        if index < len(lines) and _is_fence_closer(lines[index], opener):
            return index
    return None


def _parse_original_acceptance_items(
    value: Any, source_line: int, source_path: Path, errors: list[tuple[int, str]]
) -> tuple[AcceptanceItem, ...] | None:
    if not isinstance(value, list) or not value:
        errors.append((source_line, "original_acceptance_items must be a non-empty list"))
        return None

    items: list[AcceptanceItem] = []
    for index, raw_item in enumerate(value, start=1):
        if not isinstance(raw_item, dict):
            errors.append((source_line, f"original_acceptance_items[{index}] must be a mapping"))
            return None
        try:
            artifact_kind = ArtifactKind(str(raw_item["artifact_kind"]))
            item = AcceptanceItem(
                item_id=str(raw_item["item_id"]),
                prose=str(raw_item["prose"]),
                artifact_kind=artifact_kind,
                artifact_ref=str(raw_item["artifact_ref"]),
                source_line=source_line,
            )
        except KeyError as exc:
            errors.append(
                (source_line, f"original_acceptance_items[{index}] missing {exc.args[0]}")
            )
            return None
        except ValueError as exc:
            errors.append((source_line, f"invalid artifact_kind in {source_path}: {exc}"))
            return None
        items.append(item)
    return tuple(items)


_MANIFEST_REQUIRED_STR_FIELDS = (
    "title",
    "category",
    "task_type",
    "validation_criteria",
    "assigned_agent",
    "source_section",
)


def _resolve_manifest(
    *,
    lines: list[str],
    sections: list[PlanSection],
    plan_id: str | None,
    plan_kind: PlanKind,
    parse_mode: ParseMode,
    errors: list[tuple[int, str]],
) -> tuple[ManifestEntry, ...]:
    manifest_sections = [section for section in sections if section.kind is Kind.manifest]

    if plan_kind is PlanKind.strategy:
        for manifest in manifest_sections:
            errors.append(
                (
                    manifest.source_span[0],
                    "strategy plans must not contain a kind: manifest section",
                )
            )
        return ()

    if len(manifest_sections) > 1:
        for extra in manifest_sections[1:]:
            errors.append(
                (
                    extra.source_span[0],
                    "more than one kind: manifest section is not allowed",
                )
            )
        return ()

    if not manifest_sections:
        if parse_mode in ("expansion", "strict"):
            errors.append((max(len(lines), 1), "missing manifest"))
        return ()

    manifest_section = manifest_sections[0]
    span_start = manifest_section.source_span[0] - 1
    span_end = manifest_section.source_span[1] - 1
    block = _find_yaml_fence(lines, span_start, span_end)
    if block is None:
        errors.append((manifest_section.source_span[0], "manifest section missing YAML block"))
        return ()

    block_start, raw_block = block
    try:
        data = yaml.safe_load(raw_block)
    except yaml.YAMLError as exc:
        errors.append((block_start, f"invalid manifest YAML: {exc}"))
        return ()

    if not isinstance(data, list) or not data:
        errors.append((block_start, "manifest YAML must be a non-empty list"))
        return ()

    entries: list[ManifestEntry] = []
    for index, raw_entry in enumerate(data, start=1):
        entry = _build_manifest_entry(raw_entry, block_start, index, errors)
        if entry is not None:
            entries.append(entry)

    if entries:
        _validate_manifest_invariants(
            entries=tuple(entries),
            sections=sections,
            plan_id=plan_id,
            errors=errors,
        )

    return tuple(entries)


def _build_manifest_entry(
    raw: object,
    source_line: int,
    index: int,
    errors: list[tuple[int, str]],
) -> ManifestEntry | None:
    if not isinstance(raw, dict):
        errors.append((source_line, f"manifest entry {index} must be a mapping"))
        return None

    missing_fields = [
        field_name
        for field_name in _MANIFEST_REQUIRED_STR_FIELDS
        if not isinstance(raw.get(field_name), str) or not raw[field_name]
    ]
    if missing_fields:
        errors.append(
            (
                source_line,
                f"manifest entry {index} missing fields: {', '.join(missing_fields)}",
            )
        )
        return None

    if not isinstance(raw.get("tdd"), bool):
        errors.append((source_line, f"manifest entry {index} field 'tdd' must be a bool"))
        return None

    category = str(raw["category"])
    tdd = bool(raw["tdd"])
    if tdd and category not in TDD_ELIGIBLE_CATEGORIES:
        eligible = ", ".join(sorted(TDD_ELIGIBLE_CATEGORIES))
        errors.append(
            (
                source_line,
                f"manifest entry {index} has tdd: true for category {category!r}; "
                f"TDD is only allowed for: {eligible}",
            )
        )
        return None

    depends_on_raw = raw.get("depends_on", [])
    if not isinstance(depends_on_raw, list) or not all(
        isinstance(item, str) for item in depends_on_raw
    ):
        errors.append(
            (source_line, f"manifest entry {index} field 'depends_on' must be a list of strings")
        )
        return None

    labels_raw = raw.get("labels", [])
    if not isinstance(labels_raw, list) or not all(isinstance(item, str) for item in labels_raw):
        errors.append(
            (source_line, f"manifest entry {index} field 'labels' must be a list of strings")
        )
        return None

    return ManifestEntry(
        title=str(raw["title"]),
        category=category,
        task_type=str(raw["task_type"]),
        depends_on=tuple(depends_on_raw),
        validation_criteria=str(raw["validation_criteria"]),
        labels=tuple(labels_raw),
        assigned_agent=str(raw["assigned_agent"]),
        tdd=tdd,
        source_section=str(raw["source_section"]),
        source_line=source_line,
    )


def _validate_manifest_invariants(
    *,
    entries: tuple[ManifestEntry, ...],
    sections: list[PlanSection],
    plan_id: str | None,
    errors: list[tuple[int, str]],
) -> None:
    deliverables = {
        section.section_id: section for section in sections if section.kind is Kind.deliverable
    }

    entries_by_section: dict[str, list[ManifestEntry]] = {}
    for entry in entries:
        if entry.source_section not in deliverables:
            errors.append(
                (
                    entry.source_line,
                    f"manifest entry {entry.title!r} references unknown deliverable section "
                    f"{entry.source_section!r} (orphan)",
                )
            )
            continue
        entries_by_section.setdefault(entry.source_section, []).append(entry)

    for section_id, deliverable in deliverables.items():
        bucket = entries_by_section.get(section_id, [])
        if not bucket:
            errors.append(
                (
                    deliverable.source_span[0],
                    f"deliverable section {section_id!r} has no manifest entry",
                )
            )
        elif len(bucket) > 1:
            errors.append(
                (
                    bucket[1].source_line,
                    f"deliverable section {section_id!r} has multiple manifest entries "
                    f"({len(bucket)})",
                )
            )

    for entry in entries:
        target = deliverables.get(entry.source_section)
        if target is None:
            continue
        resolved_plan_id = resolve_plan_id(plan_id)
        expected_labels = {
            f"covers:{resolved_plan_id}:{target.section_id}:{item.item_id}"
            for item in target.acceptance_items
        }
        actual_covers = tuple(label for label in entry.labels if label.startswith("covers:"))
        actual_set = set(actual_covers)

        for label in actual_covers:
            if label not in expected_labels:
                errors.append(
                    (
                        entry.source_line,
                        f"manifest entry {entry.title!r} covers label {label!r} does not match "
                        f"any acceptance item in section {target.section_id!r}",
                    )
                )

        for label in expected_labels - actual_set:
            errors.append(
                (
                    entry.source_line,
                    f"manifest entry for section {target.section_id!r} missing covers "
                    f"label {label!r}",
                )
            )


def _parent_for_heading(section_stack: list[PlanSection], heading_level: int) -> str | None:
    for section in reversed(section_stack):
        if section.heading_level < heading_level:
            return section.section_id
    return None


def _push_section(section_stack: list[PlanSection], section: PlanSection) -> None:
    while section_stack and section_stack[-1].heading_level >= section.heading_level:
        section_stack.pop()
    section_stack.append(section)


def _clean_ref(value: str) -> str:
    cleaned = value.strip().rstrip(".;,")
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"`", '"', "'"}:
        cleaned = cleaned[1:-1]
    return cleaned.strip()


__all__ = [
    "PLAN_HEADING_REGEX",
    "AcceptanceItem",
    "ArtifactKind",
    "Deferral",
    "Kind",
    "ManifestEntry",
    "ParseMode",
    "PlanDocument",
    "PlanKind",
    "PlanParseError",
    "PlanSection",
    "MISSING_PLAN_ID_SENTINEL",
    "extract_section_dependencies",
    "parse_plan",
    "resolve_plan_id",
    "strip_section_dependencies",
]
