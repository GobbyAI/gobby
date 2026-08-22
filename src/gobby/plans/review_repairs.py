"""Typed, mechanically applicable repairs attached to plan-review findings.

This module must not import ``gobby.plans.review_findings``; that module
imports this one to validate and project the ``repairs`` field.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.symbol_targets import parse_target_line

REPAIR_KINDS = ("add_targets", "add_dependency", "add_acceptance")
REPAIR_KINDS_BY_CATEGORY: Mapping[str, frozenset[str]] = {
    "traceability": frozenset({"add_targets", "add_acceptance"}),
    "bad-sequencing": frozenset({"add_dependency"}),
    "gobby-format": frozenset(REPAIR_KINDS),
    "weak-testability": frozenset({"add_acceptance"}),
}
_PAYLOAD_KEY_BY_KIND = {
    "add_targets": "entries",
    "add_dependency": "on",
    "add_acceptance": "items",
}
_ACCEPTANCE_ITEM_KEYS = frozenset({"prose", "artifact"})
_ARTIFACT_RE = re.compile(r"^(file|symbol|test|behavior):\s*\S")

_NONEMPTY_STRING_SCHEMA = {"type": "string", "minLength": 1}
REPAIR_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "items": {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "add_targets"},
                    "section_id": _NONEMPTY_STRING_SCHEMA,
                    "entries": {
                        "type": "array",
                        "minItems": 1,
                        "items": _NONEMPTY_STRING_SCHEMA,
                    },
                },
                "required": ["kind", "section_id", "entries"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "add_dependency"},
                    "section_id": _NONEMPTY_STRING_SCHEMA,
                    "on": {
                        "type": "array",
                        "minItems": 1,
                        "items": _NONEMPTY_STRING_SCHEMA,
                        "uniqueItems": True,
                    },
                },
                "required": ["kind", "section_id", "on"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "add_acceptance"},
                    "section_id": _NONEMPTY_STRING_SCHEMA,
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "prose": _NONEMPTY_STRING_SCHEMA,
                                "artifact": _NONEMPTY_STRING_SCHEMA,
                            },
                            "required": ["prose", "artifact"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["kind", "section_id", "items"],
                "additionalProperties": False,
            },
        ]
    },
}


def validate_finding_repairs(
    raw: object,
    *,
    prefix: str,
    category: str,
    section_ids: set[str],
) -> list[dict[str, object]]:
    """Validate one finding's ``repairs`` value and return the canonical list."""
    field = f"{prefix}.repairs"
    allowed = REPAIR_KINDS_BY_CATEGORY.get(category)
    if allowed is None:
        raise _invalid(f"{field} is not allowed for category {category!r}")
    if not isinstance(raw, list) or not raw:
        raise _invalid(f"{field} must be a non-empty array")
    canonical: list[dict[str, object]] = []
    for index, entry in enumerate(raw):
        canonical.append(
            _validate_repair(
                entry,
                prefix=f"{field}[{index}]",
                allowed=allowed,
                section_ids=section_ids,
            )
        )
    return canonical


def _validate_repair(
    raw: object,
    *,
    prefix: str,
    allowed: frozenset[str],
    section_ids: set[str],
) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise _invalid(f"{prefix} must be an object")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in REPAIR_KINDS:
        raise _invalid(f"{prefix}.kind must be one of {', '.join(REPAIR_KINDS)}")
    if kind not in allowed:
        raise _invalid(f"{prefix}.kind {kind!r} is not allowed for this finding category")
    payload_key = _PAYLOAD_KEY_BY_KIND[kind]
    expected_keys = {"kind", "section_id", payload_key}
    unexpected = sorted(set(raw) - expected_keys)
    if unexpected:
        raise _invalid(f"{prefix} has unexpected keys for {kind}: {', '.join(unexpected)}")
    section_id = raw.get("section_id")
    if not isinstance(section_id, str) or not section_id.strip():
        raise _invalid(f"{prefix}.section_id must be a non-empty string")
    if section_id not in section_ids:
        raise _invalid(f"{prefix}.section_id is absent from the evidence manifest")
    if payload_key not in raw:
        raise _invalid(f"{prefix} requires {payload_key} for {kind}")
    payload_prefix = f"{prefix}.{payload_key}"
    payload = raw[payload_key]
    if not isinstance(payload, list) or not payload:
        raise _invalid(f"{payload_prefix} must be a non-empty array")

    value: list[object]
    if kind == "add_targets":
        value = list(_validate_entries(payload, prefix=payload_prefix, section_id=section_id))
    elif kind == "add_dependency":
        value = list(
            _validate_dependency_refs(
                payload,
                prefix=payload_prefix,
                section_id=section_id,
                section_ids=section_ids,
            )
        )
    else:
        value = list(_validate_acceptance_items(payload, prefix=payload_prefix))
    return {"kind": kind, "section_id": section_id, payload_key: value}


def _validate_entries(payload: list[object], *, prefix: str, section_id: str) -> list[str]:
    entries: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(payload):
        entry_prefix = f"{prefix}[{index}]"
        if not isinstance(entry, str) or not entry.strip():
            raise _invalid(f"{entry_prefix} must be a non-empty string")
        text = entry.strip()
        targets, issues = parse_target_line(text, section_id)
        if issues:
            raise _invalid(f"{entry_prefix} is not a valid target: {issues[0].message}")
        if len(targets) != 1:
            raise _invalid(f"{entry_prefix} must name exactly one target")
        reference = targets[0].reference
        if reference in seen:
            raise _invalid(f"{entry_prefix} duplicates target `{reference}`")
        seen.add(reference)
        entries.append(text)
    return entries


def _validate_dependency_refs(
    payload: list[object],
    *,
    prefix: str,
    section_id: str,
    section_ids: set[str],
) -> list[str]:
    refs: list[str] = []
    for index, ref in enumerate(payload):
        ref_prefix = f"{prefix}[{index}]"
        if not isinstance(ref, str) or not ref.strip():
            raise _invalid(f"{ref_prefix} must be a non-empty string")
        if ref not in section_ids:
            raise _invalid(f"{ref_prefix} is absent from the evidence manifest")
        if ref == section_id:
            raise _invalid(f"{ref_prefix} must not depend on its own section")
        if ref in refs:
            raise _invalid(f"{ref_prefix} duplicates dependency {ref!r}")
        refs.append(ref)
    return refs


def _validate_acceptance_items(payload: list[object], *, prefix: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for index, item in enumerate(payload):
        item_prefix = f"{prefix}[{index}]"
        if not isinstance(item, dict):
            raise _invalid(f"{item_prefix} must be an object")
        if set(item) != _ACCEPTANCE_ITEM_KEYS:
            raise _invalid(f"{item_prefix} must have exactly the keys prose and artifact")
        prose = _single_line(item["prose"], prefix=f"{item_prefix}.prose")
        artifact = _single_line(item["artifact"], prefix=f"{item_prefix}.artifact")
        if _ARTIFACT_RE.match(artifact) is None:
            raise _invalid(
                f"{item_prefix}.artifact must start with file:, symbol:, test:, or behavior:"
            )
        items.append({"prose": prose, "artifact": artifact})
    return items


def _single_line(value: object, *, prefix: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{prefix} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise _invalid(f"{prefix} must be a single line")
    return value.strip()


def _invalid(message: str) -> ReviewEvidenceError:
    return ReviewEvidenceError("invalid_round_result", message)
