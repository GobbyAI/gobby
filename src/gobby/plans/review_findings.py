"""Validation and rendering for structured plan-review rejection findings."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    ReviewEvidenceError,
    canonical_json_bytes,
    canonical_json_object,
)
from gobby.plans.review_repairs import REPAIR_SCHEMA, validate_finding_repairs

FINDING_SEVERITIES = frozenset({"blocking", "nit"})
FINDING_CATEGORIES = frozenset(
    {
        "missing-requirement",
        "bad-sequencing",
        "unhandled-edge",
        "weak-testability",
        "traceability",
        "over-engineering",
        "gobby-format",
    }
)
_REQUIRED_STRING_FIELDS = (
    "finding_id",
    "section_id",
    "check_key",
    "severity",
    "category",
    "location",
    "description",
    "fix",
    "prevention",
)
_OPTIONAL_STRING_FIELDS = ("principle", "root_cause", "causal_finding_id")
_SECTION_SET_FIELDS = ("participating_section_ids", "causal_section_ids")
_ALLOWED_FIELDS = frozenset(
    {
        *_REQUIRED_STRING_FIELDS,
        *_OPTIONAL_STRING_FIELDS,
        *_SECTION_SET_FIELDS,
        "introduced_in_round",
        "repairs",
    }
)
_STRING_SCHEMA = {"type": "string"}
_SECTION_SET_SCHEMA = {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
FINDING_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "finding_id": _STRING_SCHEMA,
        "section_id": _STRING_SCHEMA,
        "check_key": _STRING_SCHEMA,
        "severity": {"type": "string", "enum": sorted(FINDING_SEVERITIES)},
        "category": {"type": "string", "enum": sorted(FINDING_CATEGORIES)},
        "location": _STRING_SCHEMA,
        "description": _STRING_SCHEMA,
        "fix": _STRING_SCHEMA,
        "prevention": _STRING_SCHEMA,
        "principle": _STRING_SCHEMA,
        "root_cause": _STRING_SCHEMA,
        "introduced_in_round": {"type": "integer", "minimum": 1},
        "causal_finding_id": _STRING_SCHEMA,
        "participating_section_ids": _SECTION_SET_SCHEMA,
        "causal_section_ids": _SECTION_SET_SCHEMA,
        "repairs": REPAIR_SCHEMA,
    },
    "required": list(_REQUIRED_STRING_FIELDS),
    "additionalProperties": False,
}


def validate_plan_review_findings(
    raw_findings: Sequence[Mapping[str, object]],
    *,
    evidence: PlanReviewEvidence,
) -> list[dict[str, object]]:
    """Validate and canonicalize findings against server-owned evidence."""
    canonical = canonical_json_object({"findings": list(raw_findings)})["findings"]
    if not isinstance(canonical, list):
        raise _invalid("findings must be an array")
    section_ids = {section.section_id for section in evidence.section_manifest}
    findings: list[dict[str, object]] = []
    finding_ids: set[str] = set()
    for index, raw in enumerate(canonical):
        if not isinstance(raw, dict):
            raise _invalid(f"findings[{index}] must be an object")
        finding = _validate_finding(
            raw,
            index=index,
            section_ids=section_ids,
        )
        finding_id = str(finding["finding_id"])
        if finding_id in finding_ids:
            raise _invalid(f"duplicate finding_id: {finding_id}")
        finding_ids.add(finding_id)
        findings.append(finding)
    return findings


def render_rejection_section(
    *,
    round_number: int,
    findings: Sequence[Mapping[str, object]],
    evidence: PlanReviewEvidence,
) -> str:
    """Render the human projection plus canonical server-owned JSON fence."""
    lines = [f"## Adversary Findings — Round {round_number}", ""]
    for finding in findings:
        lines.extend(
            [
                (
                    f"### {finding['finding_id']} — {finding['severity']} — "
                    f"{finding['category']} — {finding['location']}"
                ),
                "",
                str(finding["description"]),
                "",
                f"**Fix:** {finding['fix']}",
                "",
                f"**Prevention:** {finding['prevention']}",
                "",
            ]
        )
        repairs = finding.get("repairs")
        if isinstance(repairs, list) and repairs:
            lines.append("**Repairs:**")
            lines.extend(_render_repair(repair) for repair in repairs)
            lines.append("")
    envelope = {
        "evidence_id": evidence.evidence_id,
        "findings": list(findings),
        "plan_hash": evidence.plan_hash,
        "section_manifest": [section.to_dict() for section in evidence.section_manifest],
    }
    lines.extend(
        [
            "```json",
            canonical_json_bytes(envelope).decode("utf-8"),
            "```",
        ]
    )
    return "\n".join(lines)


def _validate_finding(
    raw: dict[str, object],
    *,
    index: int,
    section_ids: set[str],
) -> dict[str, object]:
    prefix = f"findings[{index}]"
    unknown = sorted(set(raw) - _ALLOWED_FIELDS)
    if unknown:
        raise _invalid(f"{prefix} has unknown fields: {', '.join(unknown)}")
    for field in _REQUIRED_STRING_FIELDS:
        _require_nonempty_string(raw, field, prefix=prefix)
    for field in _OPTIONAL_STRING_FIELDS:
        if field in raw:
            _require_nonempty_string(raw, field, prefix=prefix)
    if raw["severity"] not in FINDING_SEVERITIES:
        raise _invalid(f"{prefix}.severity must be 'blocking' or 'nit'")
    if raw["category"] not in FINDING_CATEGORIES:
        raise _invalid(f"{prefix}.category is not a supported adversary category")
    if raw["section_id"] not in section_ids:
        raise _invalid(f"{prefix}.section_id is absent from the evidence manifest")
    if not raw.get("principle") and not raw.get("root_cause"):
        raise _invalid(f"{prefix} requires principle or root_cause")

    for field in _SECTION_SET_FIELDS:
        if field in raw:
            raw[field] = _validate_section_set(
                raw[field],
                field=f"{prefix}.{field}",
                section_ids=section_ids,
            )

    if "repairs" in raw:
        raw["repairs"] = validate_finding_repairs(
            raw["repairs"],
            prefix=prefix,
            category=str(raw["category"]),
            section_ids=section_ids,
        )

    causal_fields = {"introduced_in_round", "causal_finding_id", "causal_section_ids"}
    supplied_causal_fields = causal_fields.intersection(raw)
    if supplied_causal_fields and supplied_causal_fields != causal_fields:
        missing = sorted(causal_fields - supplied_causal_fields)
        raise _invalid(f"{prefix} has incomplete causal evidence: {', '.join(missing)}")
    if supplied_causal_fields:
        introduced = raw["introduced_in_round"]
        if not isinstance(introduced, int) or isinstance(introduced, bool) or introduced < 1:
            raise _invalid(f"{prefix}.introduced_in_round must be a positive integer")
    return raw


def _render_repair(repair: object) -> str:
    if not isinstance(repair, Mapping):
        return f"- {repair}"
    kind = repair.get("kind")
    section_id = repair.get("section_id")
    if kind == "add_acceptance":
        items = repair.get("items")
        rendered = (
            "; ".join(
                f"{item.get('prose')}. {item.get('artifact')}"
                for item in items
                if isinstance(item, Mapping)
            )
            if isinstance(items, list)
            else ""
        )
    else:
        payload = repair.get("entries" if kind == "add_targets" else "on")
        rendered = ", ".join(str(value) for value in payload) if isinstance(payload, list) else ""
    return f"- {kind} {section_id}: {rendered}"


def _validate_section_set(
    raw: object,
    *,
    field: str,
    section_ids: set[str],
) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise _invalid(f"{field} must be a non-empty array")
    if any(not isinstance(value, str) or not value.strip() for value in raw):
        raise _invalid(f"{field} entries must be non-empty strings")
    values = [str(value) for value in raw]
    if len(values) != len(set(values)):
        raise _invalid(f"{field} entries must be unique")
    unknown = sorted(set(values) - section_ids)
    if unknown:
        raise _invalid(f"{field} contains ids absent from evidence: {', '.join(unknown)}")
    return values


def _require_nonempty_string(
    raw: Mapping[str, object],
    field: str,
    *,
    prefix: str,
) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{prefix}.{field} must be a non-empty string")
    return value


def _invalid(message: str) -> ReviewEvidenceError:
    return ReviewEvidenceError("invalid_review_findings", message)
