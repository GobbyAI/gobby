"""Coverage label parsing and validation for implementation plans."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from gobby.plans.parser import AcceptanceItem, ArtifactKind, PlanDocument, PlanSection

COVERS_LABEL_REGEX: re.Pattern[str] = re.compile(
    r"^covers:(?P<plan_id>[A-Za-z0-9._-]+):"
    r"(?P<section_id>(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?)):"
    r"(?P<item_id>(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?))$"
)

_PATH_RE = re.compile(r"(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.:-]+)")


@dataclass(frozen=True)
class CoversRecord:
    plan_id: str
    section_id: str
    item_id: str


class InvalidCoversLabelError(ValueError):
    """Raised when a task label is not a structured covers label."""

    def __init__(self, label: str) -> None:
        self.label = label
        super().__init__(f"invalid covers label: {label!r}")


@dataclass(frozen=True)
class CoversValidationResult:
    record: CoversRecord
    leaf_task_ref: str
    status: Literal["valid", "missing_section", "missing_item", "artifact_not_referenced"]
    detail: str


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

    if not _artifact_referenced(item, leaf_validation_criteria):
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


def _artifact_referenced(item: AcceptanceItem, validation_criteria: str) -> bool:
    if item.artifact_kind is ArtifactKind.file:
        return _file_referenced(item.artifact_ref, validation_criteria)
    if item.artifact_kind is ArtifactKind.symbol:
        return _symbol_referenced(item.artifact_ref, validation_criteria)
    if item.artifact_kind is ArtifactKind.test:
        return _test_referenced(item.artifact_ref, validation_criteria)
    if item.artifact_kind is ArtifactKind.behavior:
        return _behavior_referenced(item, validation_criteria)
    return False


def _file_referenced(artifact_ref: str, validation_criteria: str) -> bool:
    return any(
        _contains_ref(validation_criteria, candidate) for candidate in _path_candidates(artifact_ref)
    )


def _symbol_referenced(artifact_ref: str, validation_criteria: str) -> bool:
    candidates = {artifact_ref}
    if "." in artifact_ref:
        candidates.add(artifact_ref.rsplit(".", maxsplit=1)[-1])
    return any(_contains_ref(validation_criteria, candidate) for candidate in candidates if candidate)


def _test_referenced(artifact_ref: str, validation_criteria: str) -> bool:
    if _contains_ref(validation_criteria, artifact_ref):
        return True
    if "::" not in artifact_ref:
        return False
    path, test_name = artifact_ref.split("::", maxsplit=1)
    return _contains_ref(validation_criteria, path) and _contains_ref(
        validation_criteria, test_name
    )


def _behavior_referenced(item: AcceptanceItem, validation_criteria: str) -> bool:
    if not _contains_ref(validation_criteria, item.artifact_ref, case_sensitive=False):
        return False
    path_candidates = _extract_path_candidates(item.prose) | _extract_path_candidates(
        item.artifact_ref
    )
    if not path_candidates:
        return False
    return any(_contains_ref(validation_criteria, candidate) for candidate in path_candidates)


def _contains_ref(text: str, ref: str, *, case_sensitive: bool = True) -> bool:
    if not ref:
        return False
    if case_sensitive:
        if ref in text:
            return True
        return re.search(re.escape(ref), text) is not None

    if ref.lower() in text.lower():
        return True
    return re.search(re.escape(ref), text, flags=re.IGNORECASE) is not None


def _path_candidates(artifact_ref: str) -> set[str]:
    cleaned = artifact_ref.strip().strip("`").strip('"').strip("'")
    candidates = {cleaned}
    if cleaned.startswith("./"):
        candidates.add(cleaned[2:])

    path = Path(cleaned)
    if path.is_absolute():
        for anchor in ("src", "tests", "docs", "web", "schemas", ".gobby"):
            if anchor in path.parts:
                candidates.add("/".join(path.parts[path.parts.index(anchor) :]))
                break
    return {candidate for candidate in candidates if candidate}


def _extract_path_candidates(text: str) -> set[str]:
    candidates: set[str] = set()
    for match in _PATH_RE.finditer(text):
        candidates.update(_path_candidates(match.group("path").rstrip(".,;)")))
    return candidates


__all__ = [
    "COVERS_LABEL_REGEX",
    "CoversRecord",
    "CoversValidationResult",
    "InvalidCoversLabelError",
    "parse_covers_label",
    "validate_covers",
]
