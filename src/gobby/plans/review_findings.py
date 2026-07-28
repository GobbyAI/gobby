"""Validation and rendering for structured plan-review rejection findings."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    ReviewEvidenceError,
    canonical_json_bytes,
    canonical_json_object,
)
from gobby.plans.review_requirements import (
    requirements_bundle_from_context,
    validate_source_citation,
)

FINDING_SEVERITIES = frozenset({"blocking", "major", "minor", "nit"})
FINDING_REPAIR_SCOPES = frozenset({"existing_sections", "new_deliverable"})
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
CHECK_KEY_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_REQUIRED_STRING_FIELDS = (
    "finding_id",
    "section_id",
    "check_key",
    "severity",
    "category",
    "location",
    "description",
    "minimal_repair",
    "repair_scope",
    "prevention",
)
_OPTIONAL_STRING_FIELDS = (
    "principle",
    "root_cause",
    "causal_finding_id",
    "new_deliverable_justification",
)
_SECTION_SET_FIELDS = ("participating_section_ids", "causal_section_ids")
_FAILURE_TRACE_STRING_FIELDS = (
    "preconditions",
    "action",
    "wrong_outcome",
    "violated_obligation",
)
_FAILURE_TRACE_FIELDS = (*_FAILURE_TRACE_STRING_FIELDS, "citation")
_ALLOWED_FIELDS = frozenset(
    {
        *_REQUIRED_STRING_FIELDS,
        *_OPTIONAL_STRING_FIELDS,
        *_SECTION_SET_FIELDS,
        "failure_trace",
        "introduced_in_round",
    }
)


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
    requirements_bundle = requirements_bundle_from_context(evidence.prior_round_context)
    findings: list[dict[str, object]] = []
    finding_ids: set[str] = set()
    for index, raw in enumerate(canonical):
        if not isinstance(raw, dict):
            raise _invalid(f"findings[{index}] must be an object")
        finding = _validate_finding(
            raw,
            index=index,
            section_ids=section_ids,
            requirements_bundle=requirements_bundle,
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
                f"**Minimal repair:** {finding['minimal_repair']}",
                "",
                f"**Repair scope:** {finding['repair_scope']}",
                "",
            ]
        )
        if "new_deliverable_justification" in finding:
            lines.extend(
                [
                    (
                        "**New deliverable justification:** "
                        f"{finding['new_deliverable_justification']}"
                    ),
                    "",
                ]
            )
        lines.extend([f"**Prevention:** {finding['prevention']}", ""])
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
    requirements_bundle: Mapping[str, object] | None,
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
        vocabulary = ", ".join(sorted(FINDING_SEVERITIES))
        raise _invalid(f"{prefix}.severity must be one of: {vocabulary}")
    if raw["category"] not in FINDING_CATEGORIES:
        raise _invalid(f"{prefix}.category is not a supported adversary category")
    if raw["repair_scope"] not in FINDING_REPAIR_SCOPES:
        vocabulary = ", ".join(sorted(FINDING_REPAIR_SCOPES))
        raise _invalid(f"{prefix}.repair_scope must be one of: {vocabulary}")
    has_new_deliverable_justification = "new_deliverable_justification" in raw
    if raw["repair_scope"] == "new_deliverable" and not has_new_deliverable_justification:
        raise _invalid(
            f"{prefix}.new_deliverable_justification is required for new_deliverable repairs"
        )
    if raw["repair_scope"] == "existing_sections" and has_new_deliverable_justification:
        raise _invalid(
            f"{prefix}.new_deliverable_justification is forbidden for existing_sections repairs"
        )
    if CHECK_KEY_RE.fullmatch(str(raw["check_key"])) is None:
        raise _invalid(f"{prefix}.check_key is invalid")
    if raw["section_id"] not in section_ids:
        raise _invalid(f"{prefix}.section_id is absent from the evidence manifest")
    if not raw.get("principle") and not raw.get("root_cause"):
        raise _invalid(f"{prefix} requires principle or root_cause")

    if raw["severity"] == "blocking" and "failure_trace" not in raw:
        fields = ", ".join(_FAILURE_TRACE_FIELDS)
        raise _invalid(
            f"{prefix}.failure_trace is required for blocking findings; "
            f"missing sub-fields: {fields}"
        )
    if "failure_trace" in raw:
        raw["failure_trace"] = _validate_failure_trace(
            raw["failure_trace"],
            prefix=f"{prefix}.failure_trace",
            requirements_bundle=requirements_bundle,
        )

    for field in _SECTION_SET_FIELDS:
        if field in raw:
            raw[field] = _validate_section_set(
                raw[field],
                field=f"{prefix}.{field}",
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


def _validate_failure_trace(
    raw: object,
    *,
    prefix: str,
    requirements_bundle: Mapping[str, object] | None,
) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise _invalid(f"{prefix} must be an object")
    trace = dict(raw)
    unknown = sorted(set(trace) - set(_FAILURE_TRACE_FIELDS))
    if unknown:
        raise _invalid(f"{prefix} has unknown fields: {', '.join(unknown)}")
    missing = [field for field in _FAILURE_TRACE_FIELDS if field not in trace]
    if missing:
        raise _invalid(f"{prefix}.{missing[0]} is required")
    for field in _FAILURE_TRACE_STRING_FIELDS:
        _require_nonempty_string(trace, field, prefix=prefix)
    trace["citation"] = _validate_citation_list(
        trace["citation"],
        prefix=f"{prefix}.citation",
        requirements_bundle=requirements_bundle,
    )
    return trace


def _validate_citation_list(
    raw: object,
    *,
    prefix: str,
    requirements_bundle: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    if not isinstance(raw, list) or not raw:
        raise _invalid(f"{prefix} must be a non-empty array")
    citations: list[dict[str, object]] = []
    for index, item in enumerate(raw):
        item_prefix = f"{prefix}[{index}]"
        citations.append(
            validate_source_citation(
                item,
                requirements_bundle=requirements_bundle,
                owner=item_prefix,
            )
        )
    return citations


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
