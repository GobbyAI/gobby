"""Manifest section parsing for implementation plans."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import yaml

from gobby.tasks.categories import (
    DEVELOPMENT_FORWARD_LEAF_CATEGORIES,
    IMPLEMENTATION_DOMAINS,
    TDD_ELIGIBLE_CATEGORIES,
)

FindYamlFence = Callable[[list[str], int, int], tuple[int, str] | None]

_MANIFEST_REQUIRED_STR_FIELDS = (
    "title",
    "category",
    "task_type",
    "validation_criteria",
    "source_section",
)


@dataclass(frozen=True)
class ManifestEntry:
    title: str
    category: str
    task_type: str
    depends_on: tuple[str, ...]
    validation_criteria: str
    labels: tuple[str, ...]
    assigned_agent: str | None
    implementation_domain: str | None
    tdd: bool
    source_section: str
    source_line: int


def resolve_manifest(
    *,
    lines: list[str],
    sections: Sequence[object],
    plan_id: str | None,
    plan_kind: str,
    parse_mode: str,
    errors: list[tuple[int, str]],
    find_yaml_fence: FindYamlFence,
) -> tuple[ManifestEntry, ...]:
    manifest_sections = [section for section in sections if _section_kind(section) == "manifest"]

    if plan_kind == "strategy":
        for manifest in manifest_sections:
            errors.append(
                (
                    _section_span(manifest)[0],
                    "strategy plans must not contain a kind: manifest section",
                )
            )
        return ()

    if len(manifest_sections) > 1:
        for extra in manifest_sections[1:]:
            errors.append(
                (_section_span(extra)[0], "more than one kind: manifest section is not allowed")
            )
        return ()

    if not manifest_sections:
        if parse_mode in ("expansion", "strict"):
            errors.append((max(len(lines), 1), "missing manifest"))
        return ()

    manifest_section = manifest_sections[0]
    span_start, span_end = _section_span(manifest_section)
    block = find_yaml_fence(lines, span_start - 1, span_end - 1)
    if block is None:
        errors.append((span_start, "manifest section missing YAML block"))
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

    missing = [
        field_name
        for field_name in _MANIFEST_REQUIRED_STR_FIELDS
        if not isinstance(raw.get(field_name), str) or not raw[field_name]
    ]
    if missing:
        errors.append((source_line, f"manifest entry {index} missing fields: {', '.join(missing)}"))
        return None

    if not isinstance(raw.get("tdd"), bool):
        errors.append((source_line, f"manifest entry {index} field 'tdd' must be a bool"))
        return None

    category = str(raw["category"])
    if category not in DEVELOPMENT_FORWARD_LEAF_CATEGORIES:
        allowed = ", ".join(sorted(DEVELOPMENT_FORWARD_LEAF_CATEGORIES))
        errors.append(
            (
                source_line,
                f"manifest entry {index} has unsupported category {category!r}; "
                f"expansion manifests only support development-forward categories: {allowed}",
            )
        )
        return None

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

    implementation_domain = raw.get("implementation_domain")
    if implementation_domain is not None:
        if not isinstance(implementation_domain, str) or not implementation_domain:
            errors.append(
                (
                    source_line,
                    f"manifest entry {index} field 'implementation_domain' must be a string",
                )
            )
            return None
        if implementation_domain not in IMPLEMENTATION_DOMAINS:
            allowed = ", ".join(sorted(IMPLEMENTATION_DOMAINS))
            errors.append(
                (
                    source_line,
                    f"manifest entry {index} has unsupported implementation_domain "
                    f"{implementation_domain!r}; expected one of: {allowed}",
                )
            )
            return None
    elif category == "code":
        errors.append(
            (source_line, f"manifest entry {index} category 'code' requires implementation_domain")
        )
        return None

    assigned_agent = raw.get("assigned_agent")
    if assigned_agent is not None and (not isinstance(assigned_agent, str) or not assigned_agent):
        errors.append(
            (
                source_line,
                f"manifest entry {index} field 'assigned_agent' "
                "must be a non-empty string when provided",
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
        assigned_agent=assigned_agent,
        implementation_domain=implementation_domain,
        tdd=tdd,
        source_section=str(raw["source_section"]),
        source_line=source_line,
    )


def _validate_manifest_invariants(
    *,
    entries: tuple[ManifestEntry, ...],
    sections: Sequence[object],
    plan_id: str | None,
    errors: list[tuple[int, str]],
) -> None:
    deliverables = {
        _section_id(section): section
        for section in sections
        if _section_kind(section) == "deliverable"
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

    valid_section_ids = {entry.source_section for entry in entries}
    for entry in entries:
        for dependency in entry.depends_on:
            if dependency not in valid_section_ids:
                errors.append(
                    (
                        entry.source_line,
                        f"manifest entry source_section={entry.source_section!r} depends on "
                        f"{dependency!r}, which has no manifest entry",
                    )
                )

    for section_id, deliverable in deliverables.items():
        bucket = entries_by_section.get(section_id, [])
        if not bucket:
            errors.append(
                (
                    _section_span(deliverable)[0],
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
        expected_labels = {
            f"covers:{plan_id or 'unknown'}:{_section_id(target)}:{_item_id(item)}"
            for item in _acceptance_items(target)
        }
        actual_covers = tuple(label for label in entry.labels if label.startswith("covers:"))
        actual_set = set(actual_covers)

        for label in actual_covers:
            if label not in expected_labels:
                errors.append(
                    (
                        entry.source_line,
                        f"manifest entry {entry.title!r} covers label {label!r} does not match "
                        f"any acceptance item in section {_section_id(target)!r}",
                    )
                )

        for label in expected_labels - actual_set:
            errors.append(
                (
                    entry.source_line,
                    f"manifest entry for section {_section_id(target)!r} missing covers "
                    f"label {label!r}",
                )
            )


def _section_kind(section: object) -> str:
    return str(getattr(section, "kind", ""))


def _section_id(section: object) -> str:
    return str(getattr(section, "section_id", ""))


def _section_span(section: object) -> tuple[int, int]:
    span = getattr(section, "source_span", (1, 1))
    return int(span[0]), int(span[1])


def _acceptance_items(section: object) -> tuple[object, ...]:
    items = getattr(section, "acceptance_items", ())
    return tuple(items) if isinstance(items, Sequence) else ()


def _item_id(item: object) -> str:
    return str(getattr(item, "item_id", ""))


__all__ = ["ManifestEntry", "resolve_manifest"]
