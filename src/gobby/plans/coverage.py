"""Coverage label parsing, validation, and report evaluation for implementation plans."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

import yaml

from gobby.plans._artifact_refs import artifact_referenced
from gobby.plans.evidence import EvidenceKind, EvidenceRow
from gobby.plans.parser import AcceptanceItem, PlanDocument, PlanSection, parse_plan

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol

_DOTTED_ID_PATTERN = (
    r"(?:\d+[a-z]?|[A-Z]+[0-9]+[a-z]?)(?:\.(?:\d+[a-z]?|[A-Z]+[0-9]+[a-z]?))*"
)

COVERS_LABEL_REGEX: re.Pattern[str] = re.compile(
    r"^covers:(?P<plan_id>[A-Za-z0-9._-]+):"
    rf"(?P<section_id>{_DOTTED_ID_PATTERN}):"
    rf"(?P<item_id>{_DOTTED_ID_PATTERN})$"
)
type CoversStatus = Literal["valid", "missing_section", "missing_item", "artifact_not_referenced"]
type PlanInput = PlanDocument | Path | str


@dataclass(frozen=True)
class CoversRecord:
    plan_id: str
    section_id: str
    item_id: str


class InvalidCoversLabelError(ValueError):
    """Raised when a task label does not match the covers label grammar."""

    def __init__(self, label: str) -> None:
        super().__init__(f"invalid covers label: {label!r}")


@dataclass(frozen=True)
class CoversValidationResult:
    record: CoversRecord
    leaf_task_ref: str
    status: CoversStatus
    detail: str


class CoverageStatus(StrEnum):
    covered = "covered"
    deferred = "deferred"
    missing = "missing"
    invalid = "invalid"


class TaskTreeSource(StrEnum):
    db = "db"
    matrix_file = "matrix-file"


class MissingScopeError(ValueError):
    """Raised when the selected task-tree source is missing required scope."""


class StaleHashError(ValueError):
    """Raised when the supplied plan hash no longer matches the parsed plan."""


@dataclass(frozen=True)
class CoverageRowLeaf:
    leaf_task_ref: str
    validation_criteria_snippet: str
    matched_artifact_ref: str


@dataclass(frozen=True)
class CoverageHeader:
    plan_id: str
    plan_hash: str
    root_task_ref: str | None
    project_id: str | None
    generated_at: str
    task_tree_source: TaskTreeSource
    task_tree_source_hash: str
    evidence_summary: tuple[str, ...]


@dataclass(frozen=True)
class CoverageRow:
    section_id: str
    item_id: str
    status: CoverageStatus
    leaves: tuple[CoverageRowLeaf, ...] = ()
    deferral_target: str | None = None
    evidence: tuple[EvidenceRow, ...] = ()


@dataclass(frozen=True)
class CoverageReport:
    header: CoverageHeader
    rows: tuple[CoverageRow, ...]

    @property
    def has_missing(self) -> bool:
        return any(row.status is CoverageStatus.missing for row in self.rows)

    @property
    def has_invalid(self) -> bool:
        return any(row.status is CoverageStatus.invalid for row in self.rows)

    @property
    def is_complete(self) -> bool:
        return not self.has_missing and not self.has_invalid


@dataclass(frozen=True)
class _TaskRecord:
    ref: str
    labels: tuple[str, ...]
    validation_criteria: str
    status: str
    parent_ref: str | None = None
    path_cache: str | None = None
    dependencies: tuple[str, ...] = ()


class _TaskRecordStore:
    def __init__(self, records: Sequence[_TaskRecord]) -> None:
        self._by_ref = {record.ref: record for record in records}

    def get_task(self, task_ref: str) -> dict[str, object] | None:
        record = self._by_ref.get(_normalize_ref(task_ref))
        if record is None:
            return None
        return {
            "status": record.status,
            "validation_criteria": record.validation_criteria,
            "labels": list(record.labels),
            "dependencies": list(record.dependencies),
        }

    def get_task_labels(self, task_ref: str) -> list[str]:
        record = self._by_ref.get(_normalize_ref(task_ref))
        return list(record.labels) if record is not None else []

    def get_task_dependencies(self, task_ref: str) -> list[str]:
        record = self._by_ref.get(_normalize_ref(task_ref))
        return list(record.dependencies) if record is not None else []


def parse_covers_label(label: str) -> CoversRecord:
    match = COVERS_LABEL_REGEX.match(label)
    if match is None:
        raise InvalidCoversLabelError(label)
    return CoversRecord(
        plan_id=match.group("plan_id"),
        section_id=match.group("section_id"),
        item_id=match.group("item_id"),
    )


def validate_covers(
    record: CoversRecord,
    leaf_validation_criteria: str,
    leaf_task_ref: str,
    plan_doc: PlanDocument,
) -> CoversValidationResult:
    section = _find_section(plan_doc, record.section_id)
    if section is None:
        return CoversValidationResult(
            record=record,
            leaf_task_ref=leaf_task_ref,
            status="missing_section",
            detail=f"section {record.section_id!r} is not present in plan {record.plan_id!r}",
        )

    item = _find_acceptance_item(section, record.item_id)
    if item is None:
        return CoversValidationResult(
            record=record,
            leaf_task_ref=leaf_task_ref,
            status="missing_item",
            detail=f"item {record.item_id!r} is not present in section {record.section_id!r}",
        )

    if not artifact_referenced(item, leaf_validation_criteria):
        return CoversValidationResult(
            record=record,
            leaf_task_ref=leaf_task_ref,
            status="artifact_not_referenced",
            detail=(
                f"leaf {leaf_task_ref!r} does not reference artifact "
                f"{item.artifact_ref!r} for item {record.item_id!r}"
            ),
        )

    return CoversValidationResult(
        record=record,
        leaf_task_ref=leaf_task_ref,
        status="valid",
        detail=f"leaf {leaf_task_ref!r} covers item {record.item_id!r}",
    )


@overload
def evaluate(
    *,
    plan: PlanInput,
    plan_id: str,
    plan_hash: str,
    task_tree: Literal[TaskTreeSource.db, "db"],
    root_task_ref: str,
    project_id: str,
    db: DatabaseProtocol | None = None,
    task_records: Sequence[Mapping[str, object]] | None = None,
    task_tree_file: Path | str | None = None,
    evidence: Sequence[EvidenceRow] | None = None,
    recovery_epic_ref: str | None = None,
) -> CoverageReport: ...


@overload
def evaluate(
    *,
    plan: PlanInput,
    plan_id: str,
    plan_hash: str,
    task_tree: Literal[TaskTreeSource.matrix_file, "matrix-file", "matrix_file"],
    matrix_file: Path | str,
    evidence: Sequence[EvidenceRow] | None = None,
) -> CoverageReport: ...


def evaluate(
    *,
    plan: PlanInput,
    plan_id: str,
    plan_hash: str,
    task_tree: TaskTreeSource | str,
    root_task_ref: str | None = None,
    project_id: str | None = None,
    matrix_file: Path | str | None = None,
    db: DatabaseProtocol | None = None,
    task_records: Sequence[Mapping[str, object]] | None = None,
    task_tree_file: Path | str | None = None,
    evidence: Sequence[EvidenceRow] | None = None,
    recovery_epic_ref: str | None = None,
) -> CoverageReport:
    source = _task_tree_source(task_tree)
    plan_doc = _load_plan(plan)
    _ensure_fresh_plan_hash(plan_doc, plan_hash)
    evidence_rows = tuple(evidence or ())

    if source is TaskTreeSource.matrix_file:
        if root_task_ref is not None or project_id is not None:
            raise MissingScopeError("matrix-file coverage does not accept root_task_ref/project_id")
        if matrix_file is None:
            raise MissingScopeError("matrix-file coverage requires matrix_file")
        return _evaluate_matrix_file(
            Path(matrix_file),
            plan_id=plan_id,
            plan_hash=plan_hash,
            plan_doc=plan_doc,
            evidence=evidence_rows,
        )

    if not root_task_ref:
        raise MissingScopeError(f"{source.value} coverage requires root_task_ref")
    if not project_id:
        raise MissingScopeError(f"{source.value} coverage requires project_id")
    if matrix_file is not None:
        raise MissingScopeError(f"{source.value} coverage does not accept matrix_file")

    records = _load_task_records(
        source,
        project_id=project_id,
        db=db,
        task_records=task_records,
        task_tree_file=task_tree_file,
    )
    scoped_records = _filter_to_scope(records, root_task_ref)
    return _evaluate_records(
        plan_doc=plan_doc,
        plan_id=plan_id,
        plan_hash=plan_hash,
        task_tree_source=source,
        task_tree_source_hash=_records_hash(scoped_records),
        root_task_ref=root_task_ref,
        project_id=project_id,
        records=scoped_records,
        evidence=evidence_rows,
        recovery_epic_ref=recovery_epic_ref or root_task_ref,
    )


def _evaluate_records(
    *,
    plan_doc: PlanDocument,
    plan_id: str,
    plan_hash: str,
    task_tree_source: TaskTreeSource,
    task_tree_source_hash: str,
    root_task_ref: str,
    project_id: str,
    records: Sequence[_TaskRecord],
    evidence: tuple[EvidenceRow, ...],
    recovery_epic_ref: str,
) -> CoverageReport:
    store = _TaskRecordStore(records)
    rows: list[CoverageRow] = []
    for section in plan_doc.sections:
        for item in section.acceptance_items:
            rows.append(
                _evaluate_item(
                    plan_doc=plan_doc,
                    plan_id=plan_id,
                    section=section,
                    item=item,
                    records=records,
                    store=store,
                    evidence=evidence,
                    recovery_epic_ref=recovery_epic_ref,
                )
            )

    return CoverageReport(
        header=CoverageHeader(
            plan_id=plan_id,
            plan_hash=plan_hash,
            root_task_ref=root_task_ref,
            project_id=project_id,
            generated_at=_utc_now(),
            task_tree_source=task_tree_source,
            task_tree_source_hash=task_tree_source_hash,
            evidence_summary=_evidence_summary(evidence),
        ),
        rows=tuple(rows),
    )


def _evaluate_item(
    *,
    plan_doc: PlanDocument,
    plan_id: str,
    section: PlanSection,
    item: AcceptanceItem,
    records: Sequence[_TaskRecord],
    store: _TaskRecordStore,
    evidence: tuple[EvidenceRow, ...],
    recovery_epic_ref: str,
) -> CoverageRow:
    invalid_leaves: list[CoverageRowLeaf] = []
    covered_leaves: list[CoverageRowLeaf] = []
    for record, task in _matching_cover_records(records, plan_id, section.section_id, item.item_id):
        result = validate_covers(record, task.validation_criteria, task.ref, plan_doc)
        leaf = CoverageRowLeaf(
            leaf_task_ref=task.ref,
            validation_criteria_snippet=_snippet(task.validation_criteria),
            matched_artifact_ref=item.artifact_ref,
        )
        if result.status == "valid":
            covered_leaves.append(leaf)
        else:
            invalid_leaves.append(leaf)

    if covered_leaves:
        return CoverageRow(
            section_id=section.section_id,
            item_id=item.item_id,
            status=CoverageStatus.covered,
            leaves=tuple(covered_leaves),
            evidence=evidence,
        )
    if invalid_leaves:
        return CoverageRow(
            section_id=section.section_id,
            item_id=item.item_id,
            status=CoverageStatus.invalid,
            leaves=tuple(invalid_leaves),
            evidence=evidence,
        )

    if section.deferral is not None and _deferral_covers_item(section, item):
        status = _validate_deferral_status(
            section=section,
            plan_id=plan_id,
            store=store,
            recovery_epic_ref=recovery_epic_ref,
        )
        return CoverageRow(
            section_id=section.section_id,
            item_id=item.item_id,
            status=CoverageStatus.deferred if status == "valid" else CoverageStatus.invalid,
            deferral_target=section.deferral.task_ref,
            evidence=evidence,
        )

    return CoverageRow(
        section_id=section.section_id,
        item_id=item.item_id,
        status=CoverageStatus.missing,
        evidence=evidence,
    )


def _evaluate_matrix_file(
    matrix_file: Path,
    *,
    plan_id: str,
    plan_hash: str,
    plan_doc: PlanDocument,
    evidence: tuple[EvidenceRow, ...],
) -> CoverageReport:
    raw = yaml.safe_load(matrix_file.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        rows: tuple[CoverageRow, ...] = ()
        header_data: dict[str, object] = {}
    else:
        rows = tuple(_row_from_manifest(row, evidence=evidence) for row in _raw_rows(raw))
        header_value = raw.get("header", {})
        header_data = header_value if isinstance(header_value, dict) else {}

    existing_hash = str(header_data.get("plan_hash", ""))
    if existing_hash and existing_hash != plan_hash:
        raise StaleHashError(
            f"coverage matrix hash {existing_hash!r} does not match plan hash {plan_hash!r}"
        )

    return CoverageReport(
        header=CoverageHeader(
            plan_id=plan_id,
            plan_hash=plan_hash,
            root_task_ref=_optional_string(header_data.get("root_task_ref")),
            project_id=_optional_string(header_data.get("project_id")),
            generated_at=_utc_now(),
            task_tree_source=TaskTreeSource.matrix_file,
            task_tree_source_hash=_file_hash(matrix_file),
            evidence_summary=_evidence_summary(evidence),
        ),
        rows=rows or _missing_rows(plan_doc, evidence=evidence),
    )


def _row_from_manifest(raw: object, *, evidence: tuple[EvidenceRow, ...]) -> CoverageRow:
    if not isinstance(raw, dict):
        return CoverageRow(
            section_id="", item_id="", status=CoverageStatus.invalid, evidence=evidence
        )
    return CoverageRow(
        section_id=str(raw.get("section_id", "")),
        item_id=str(raw.get("item_id", "")),
        status=_coverage_status(raw.get("status")),
        leaves=tuple(_leaf_from_manifest(value) for value in _sequence(raw.get("leaves"))),
        deferral_target=_optional_string(raw.get("deferral_target")),
        evidence=evidence,
    )


def _leaf_from_manifest(raw: object) -> CoverageRowLeaf:
    if not isinstance(raw, dict):
        return CoverageRowLeaf(
            leaf_task_ref="",
            validation_criteria_snippet="",
            matched_artifact_ref="",
        )
    return CoverageRowLeaf(
        leaf_task_ref=str(raw.get("leaf_task_ref", "")),
        validation_criteria_snippet=str(raw.get("validation_criteria_snippet", "")),
        matched_artifact_ref=str(raw.get("matched_artifact_ref", "")),
    )


def _missing_rows(
    plan_doc: PlanDocument, *, evidence: tuple[EvidenceRow, ...]
) -> tuple[CoverageRow, ...]:
    return tuple(
        CoverageRow(
            section_id=section.section_id,
            item_id=item.item_id,
            status=CoverageStatus.missing,
            evidence=evidence,
        )
        for section in plan_doc.sections
        for item in section.acceptance_items
    )


def _matching_cover_records(
    records: Sequence[_TaskRecord], plan_id: str, section_id: str, item_id: str
) -> list[tuple[CoversRecord, _TaskRecord]]:
    matches: list[tuple[CoversRecord, _TaskRecord]] = []
    for task in records:
        for label in task.labels:
            if not label.startswith("covers:"):
                continue
            try:
                record = parse_covers_label(label)
            except InvalidCoversLabelError:
                continue
            if (
                record.plan_id == plan_id
                and record.section_id == section_id
                and record.item_id == item_id
            ):
                matches.append((record, task))
    return matches


def _validate_deferral_status(
    *,
    section: PlanSection,
    plan_id: str,
    store: _TaskRecordStore,
    recovery_epic_ref: str,
) -> str:
    from gobby.plans.deferral import validate_deferral

    if section.deferral is None:
        return "task_missing"
    result = validate_deferral(
        section.deferral,
        plan_id,
        section.section_id,
        store,
        recovery_epic_ref=recovery_epic_ref,
    )
    return result.status


def _deferral_covers_item(section: PlanSection, item: AcceptanceItem) -> bool:
    return section.deferral is not None and any(
        original.item_id == item.item_id for original in section.deferral.original_acceptance_items
    )


def _find_section(plan_doc: PlanDocument, section_id: str) -> PlanSection | None:
    for section in plan_doc.sections:
        if section.section_id == section_id:
            return section
    return None


def _find_acceptance_item(section: PlanSection, item_id: str) -> AcceptanceItem | None:
    for item in section.acceptance_items:
        if item.item_id == item_id:
            return item
    return None


def _load_plan(plan: PlanInput) -> PlanDocument:
    if isinstance(plan, PlanDocument):
        return plan
    return parse_plan(Path(plan), parse_mode="draft")


def _ensure_fresh_plan_hash(plan_doc: PlanDocument, plan_hash: str) -> None:
    if plan_doc.source_hash != plan_hash:
        raise StaleHashError(
            f"plan hash {plan_hash!r} does not match parsed hash {plan_doc.source_hash!r}"
        )


def _task_tree_source(value: TaskTreeSource | str) -> TaskTreeSource:
    if isinstance(value, TaskTreeSource):
        return value
    normalized = value.replace("_", "-")
    try:
        return TaskTreeSource(normalized)
    except ValueError as exc:
        raise MissingScopeError(f"unknown task tree source {value!r}") from exc


def _load_task_records(
    source: TaskTreeSource,
    *,
    project_id: str,
    db: DatabaseProtocol | None,
    task_records: Sequence[Mapping[str, object]] | None,
    task_tree_file: Path | str | None,
) -> tuple[_TaskRecord, ...]:
    if task_records is not None:
        return tuple(_coerce_task_record(record) for record in task_records)

    if source is TaskTreeSource.db:
        if task_tree_file is not None:
            raise MissingScopeError("db coverage does not accept task_tree_file")
        return _load_db_task_records(project_id, db=db)

    return ()


def _load_db_task_records(
    project_id: str,
    *,
    db: DatabaseProtocol | None,
) -> tuple[_TaskRecord, ...]:
    from gobby.storage.database import LocalDatabase
    from gobby.storage.migrations import run_migrations

    if db is not None:
        return _load_db_task_records_from_connection(project_id, db=db)

    owned_db = LocalDatabase()
    try:
        run_migrations(owned_db)
        return _load_db_task_records_from_connection(project_id, db=owned_db)
    finally:
        owned_db.close()


def _load_db_task_records_from_connection(
    project_id: str,
    *,
    db: DatabaseProtocol,
) -> tuple[_TaskRecord, ...]:
    from gobby.storage.tasks import LocalTaskManager

    manager = LocalTaskManager(db)
    tasks = []
    limit = 1000
    offset = 0
    while True:
        page = manager.list_tasks(project_id=project_id, limit=limit, offset=offset)
        tasks.extend(page)
        if len(page) < limit:
            break
        offset += limit
    task_ref_by_id = {task.id: _live_task_ref(task) for task in tasks}
    return tuple(_live_task_record(task, task_ref_by_id) for task in tasks)


def _live_task_record(task: Any, task_ref_by_id: Mapping[str, str]) -> _TaskRecord:
    labels = task.labels or ()
    dependencies = tuple(
        sorted(
            task_ref_by_id[depends_on]
            for depends_on in task.blocked_by
            if depends_on in task_ref_by_id
        )
    )
    parent_ref = (
        task_ref_by_id.get(task.parent_task_id) if task.parent_task_id is not None else None
    )
    return _TaskRecord(
        ref=_live_task_ref(task),
        labels=tuple(str(label) for label in labels),
        validation_criteria=task.validation_criteria or task.description or "",
        status=str(task.status),
        parent_ref=parent_ref,
        path_cache=task.path_cache,
        dependencies=dependencies,
    )


def _live_task_ref(task: Any) -> str:
    seq_num = task.seq_num
    if isinstance(seq_num, int):
        return f"#{seq_num}"
    return str(task.id)


def _coerce_task_record(raw: Mapping[str, object]) -> _TaskRecord:
    ref = _task_ref(raw)
    return _TaskRecord(
        ref=ref,
        labels=_labels(raw.get("labels")),
        validation_criteria=_first_string(raw, "validation_criteria", "validation", "description"),
        status=_first_string(raw, "status", "lifecycle_stage", default="open"),
        parent_ref=_optional_ref(
            _first_string(raw, "parent_ref", "parent_task_ref", "parent", default="")
        ),
        path_cache=_optional_string(raw.get("path_cache")),
        dependencies=_dependency_refs(raw.get("dependencies")),
    )


def _task_ref(raw: Mapping[str, object]) -> str:
    for key in ("ref", "task_ref", "id"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return _normalize_ref(value)
    seq_num = raw.get("seq_num")
    if isinstance(seq_num, int):
        return f"#{seq_num}"
    return ""


def _labels(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tuple(label.strip() for label in raw.split(",") if label.strip())
    if isinstance(raw, Sequence):
        return tuple(str(label) for label in raw)
    return ()


def _dependency_refs(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    values: list[str] = []
    if isinstance(raw, Mapping):
        for key in ("blocked_by", "depends_on", "dependencies"):
            values.extend(_dependency_refs(raw.get(key)))
        return tuple(values)
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        for item in raw:
            if isinstance(item, str):
                values.append(_normalize_ref(item))
            elif isinstance(item, Mapping):
                ref = item.get("ref") or item.get("depends_on") or item.get("task_ref")
                if isinstance(ref, str):
                    values.append(_normalize_ref(ref))
        return tuple(values)
    if isinstance(raw, str):
        return (_normalize_ref(raw),)
    return ()


def _first_string(raw: Mapping[str, object], *keys: str, default: str = "") -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return default


def _optional_string(raw: object) -> str | None:
    if raw is None:
        return None
    value = str(raw)
    return value if value else None


def _optional_ref(raw: str) -> str | None:
    return _normalize_ref(raw) if raw else None


def _normalize_ref(ref: str) -> str:
    stripped = ref.strip()
    if stripped.isdecimal():
        return f"#{stripped}"
    return stripped


def _filter_to_scope(records: Sequence[_TaskRecord], root_task_ref: str) -> tuple[_TaskRecord, ...]:
    normalized_root = _normalize_ref(root_task_ref)
    root_key = normalized_root.lstrip("#")
    included = {
        record.ref
        for record in records
        if record.ref == normalized_root
        or record.ref.lstrip("#") == root_key
        or _path_in_scope(record.path_cache, root_key)
    }
    children_by_parent: dict[str, list[str]] = {}
    for record in records:
        if record.parent_ref is not None:
            children_by_parent.setdefault(record.parent_ref, []).append(record.ref)

    stack = list(included)
    while stack:
        parent_ref = stack.pop()
        for child_ref in children_by_parent.get(parent_ref, ()):
            if child_ref in included:
                continue
            included.add(child_ref)
            stack.append(child_ref)
    return tuple(record for record in records if record.ref in included)


def _path_in_scope(path_cache: str | None, root_key: str) -> bool:
    return path_cache == root_key or bool(path_cache and path_cache.startswith(f"{root_key}."))


def _records_hash(records: Sequence[_TaskRecord]) -> str:
    payload = [
        {
            "ref": record.ref,
            "labels": record.labels,
            "validation_criteria": record.validation_criteria,
            "status": record.status,
            "parent_ref": record.parent_ref,
            "path_cache": record.path_cache,
            "dependencies": record.dependencies,
        }
        for record in records
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _evidence_summary(evidence: Sequence[EvidenceRow]) -> tuple[str, ...]:
    return tuple(f"{row.kind.value}:{row.ref}:{row.status.value}" for row in evidence)


def _snippet(value: str, limit: int = 160) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}..."


def _raw_rows(raw: Mapping[str, object]) -> tuple[object, ...]:
    rows = raw.get("rows", ())
    if isinstance(rows, Sequence) and not isinstance(rows, str):
        return tuple(rows)
    return ()


def _sequence(raw: object) -> tuple[object, ...]:
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        return tuple(raw)
    return ()


def _coverage_status(raw: object) -> CoverageStatus:
    try:
        return CoverageStatus(str(raw))
    except ValueError:
        return CoverageStatus.invalid


__all__ = [
    "COVERS_LABEL_REGEX",
    "CoverageHeader",
    "CoverageReport",
    "CoverageRow",
    "CoverageRowLeaf",
    "CoverageStatus",
    "CoversRecord",
    "CoversValidationResult",
    "EvidenceKind",
    "InvalidCoversLabelError",
    "MissingScopeError",
    "StaleHashError",
    "TaskTreeSource",
    "evaluate",
    "parse_covers_label",
    "validate_covers",
]
