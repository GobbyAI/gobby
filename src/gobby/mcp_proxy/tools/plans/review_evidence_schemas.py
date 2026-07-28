"""Strict JSON schemas for structured plan-review evidence payloads."""

from __future__ import annotations

from gobby.plans.review_telemetry import CONVERGENCE_TELEMETRY_SCHEMA

_NONEMPTY_STRING = {"type": "string", "minLength": 1}
_SHA256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_CHECK_KEY = {
    "type": "string",
    "pattern": "^[a-z0-9]+(?:[._-][a-z0-9]+)*$",
}
_STRING_ARRAY = {
    "type": "array",
    "items": _NONEMPTY_STRING,
    "uniqueItems": True,
}

SOURCE_CITATION_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "path": _NONEMPTY_STRING,
                "sha256": _SHA256,
                "line_start": {"type": "integer", "minimum": 1},
                "line_end": {"type": "integer", "minimum": 1},
            },
            "required": ["path", "sha256"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "requirement_id": {
                    "type": "string",
                    "pattern": "^req-[0-9a-f]{12}$",
                },
                "content_sha256": _SHA256,
                "line_start": {"type": "integer", "minimum": 1},
                "line_end": {"type": "integer", "minimum": 1},
            },
            "required": ["requirement_id", "content_sha256"],
            "additionalProperties": False,
        },
    ],
}

_CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate_id": _NONEMPTY_STRING,
        "violated_invariant": _NONEMPTY_STRING,
        "suggested_fix": _NONEMPTY_STRING,
        "section_ids": {
            **_STRING_ARRAY,
            "minItems": 1,
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "source_citations": {
            "type": "array",
            "items": SOURCE_CITATION_SCHEMA,
            "minItems": 1,
        },
        "adjacent_sites_checked": _STRING_ARRAY,
    },
    "required": [
        "candidate_id",
        "violated_invariant",
        "suggested_fix",
        "section_ids",
        "confidence",
        "source_citations",
        "adjacent_sites_checked",
    ],
    "additionalProperties": False,
}

LANE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "lane_id": {
            "type": "string",
            "enum": ["requirements", "failure-paths", "integration"],
        },
        "status": {"const": "completed"},
        "section_ids_checked": {
            **_STRING_ARRAY,
            "minItems": 1,
        },
        "source_citations": {
            "type": "array",
            "items": SOURCE_CITATION_SCHEMA,
            "minItems": 1,
        },
        "candidate_issues": {
            "type": "array",
            "items": _CANDIDATE_SCHEMA,
        },
    },
    "required": [
        "lane_id",
        "status",
        "section_ids_checked",
        "source_citations",
        "candidate_issues",
    ],
    "additionalProperties": False,
}

LANE_RESULTS_SCHEMA = {
    "type": "array",
    "items": LANE_RESULT_SCHEMA,
    "minItems": 3,
    "maxItems": 3,
}

CANDIDATE_DISPOSITION_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate_id": _NONEMPTY_STRING,
        "check_key": _CHECK_KEY,
        "source_section_ids": {
            **_STRING_ARRAY,
            "minItems": 1,
        },
        "source_hash": _SHA256,
        "disposition": {
            "type": "string",
            "enum": ["emitted_finding", "dismissed"],
        },
        "rationale": _NONEMPTY_STRING,
        "finding_id": _NONEMPTY_STRING,
    },
    "required": [
        "candidate_id",
        "check_key",
        "source_section_ids",
        "source_hash",
        "disposition",
        "rationale",
    ],
    "allOf": [
        {
            "if": {
                "properties": {"disposition": {"const": "emitted_finding"}},
                "required": ["disposition"],
            },
            "then": {"required": ["finding_id"]},
            "else": {"not": {"required": ["finding_id"]}},
        }
    ],
    "additionalProperties": False,
}

_CROSS_LANE_INTERACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate_ids": {
            **_STRING_ARRAY,
            "minItems": 2,
            "maxItems": 2,
        },
        "affected_section_ids": {
            **_STRING_ARRAY,
            "minItems": 1,
        },
        "interaction_checked": _NONEMPTY_STRING,
        "disposition": _NONEMPTY_STRING,
    },
    "required": [
        "candidate_ids",
        "affected_section_ids",
        "interaction_checked",
        "disposition",
    ],
    "additionalProperties": False,
}

_ADJACENT_VARIANT_SWEEP_SCHEMA = {
    "type": "object",
    "properties": {
        "check_key": _CHECK_KEY,
        "seed_candidate_id": _NONEMPTY_STRING,
        "query_evidence": _STRING_ARRAY,
        "sites_checked": _STRING_ARRAY,
        "resulting_candidate_ids": _STRING_ARRAY,
    },
    "required": [
        "check_key",
        "seed_candidate_id",
        "query_evidence",
        "sites_checked",
        "resulting_candidate_ids",
    ],
    "additionalProperties": False,
}

_CAUSAL_REPAIR_SWEEP_SCHEMA = {
    "type": "object",
    "properties": {
        "prior_finding_id": _NONEMPTY_STRING,
        "changed_section_ids": _STRING_ARRAY,
        "changed_contracts": _STRING_ARRAY,
        "sites_checked": _STRING_ARRAY,
        "query_evidence": _STRING_ARRAY,
        "disposition": _NONEMPTY_STRING,
    },
    "required": [
        "prior_finding_id",
        "changed_section_ids",
        "changed_contracts",
        "sites_checked",
        "query_evidence",
        "disposition",
    ],
    "additionalProperties": False,
}

CANDIDATE_DISPOSITIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "cross_lane_interactions": {
            "type": "array",
            "items": _CROSS_LANE_INTERACTION_SCHEMA,
        },
        "adjacent_variant_sweeps": {
            "type": "array",
            "items": _ADJACENT_VARIANT_SWEEP_SCHEMA,
        },
        "causal_repair_sweeps": {
            "type": "array",
            "items": _CAUSAL_REPAIR_SWEEP_SCHEMA,
        },
        "candidate_dispositions": {
            "type": "array",
            "items": CANDIDATE_DISPOSITION_SCHEMA,
        },
    },
    "required": [
        "cross_lane_interactions",
        "adjacent_variant_sweeps",
        "causal_repair_sweeps",
        "candidate_dispositions",
    ],
    "additionalProperties": False,
}

ROUTING_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "assigned_agent": _NONEMPTY_STRING,
        "category": _NONEMPTY_STRING,
        "depends_on": _STRING_ARRAY,
        "implementation_domain": {
            "type": "string",
            "enum": ["frontend", "backend", "fullstack"],
        },
        "task_type": _NONEMPTY_STRING,
        "tdd": {"type": "boolean"},
    },
    "required": [],
    "additionalProperties": False,
}

ROUTING_DECISIONS_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "propertyNames": _NONEMPTY_STRING,
    "additionalProperties": ROUTING_DECISION_SCHEMA,
}

PRIOR_FINDING_RESOLUTION_SCHEMA = {
    "type": "object",
    "properties": {
        "prior_finding_id": _NONEMPTY_STRING,
        "decision": {
            "type": "string",
            "enum": ["repair", "carry"],
        },
    },
    "required": ["prior_finding_id", "decision"],
    "additionalProperties": False,
}

PRIOR_FINDING_RESOLUTIONS_SCHEMA = {
    "type": "array",
    "items": PRIOR_FINDING_RESOLUTION_SCHEMA,
}

_DEVIATION_PROOF_SCHEMA = {
    "type": ["object", "null"],
    "properties": {
        "violated_invariant": _NONEMPTY_STRING,
        "original_counterexample": _NONEMPTY_STRING,
        "how_alternative_closes_it": _NONEMPTY_STRING,
        "validation_evidence": _NONEMPTY_STRING,
        "accepted_risk": _NONEMPTY_STRING,
    },
    "required": [
        "violated_invariant",
        "original_counterexample",
        "how_alternative_closes_it",
        "validation_evidence",
        "accepted_risk",
    ],
    "additionalProperties": False,
}

_DEFERRED_SITE_SCHEMA = {
    "type": "object",
    "properties": {
        "site_id": _NONEMPTY_STRING,
        "reason": _NONEMPTY_STRING,
    },
    "required": ["site_id", "reason"],
    "additionalProperties": False,
}

_REPAIR_INTERACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "edge_id": _NONEMPTY_STRING,
        "disposition": _NONEMPTY_STRING,
        "validation_evidence": {
            **_STRING_ARRAY,
            "minItems": 1,
        },
    },
    "required": ["edge_id", "disposition", "validation_evidence"],
    "additionalProperties": False,
}

REPAIR_ATTESTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "prior_finding_id": _NONEMPTY_STRING,
        "check_key": _CHECK_KEY,
        "changed_section_ids": {
            **_STRING_ARRAY,
            "minItems": 1,
        },
        "accepted_resolution": _NONEMPTY_STRING,
        "deviation_from_minimal_repair": _DEVIATION_PROOF_SCHEMA,
        "changed_symbols": _STRING_ARRAY,
        "consumer_sites_swept": _STRING_ARRAY,
        "adjacent_variants_swept": _STRING_ARRAY,
        "validation_evidence": _STRING_ARRAY,
        "deferred_sites": {
            "type": "array",
            "items": _DEFERRED_SITE_SCHEMA,
        },
        "repair_universe_digest": _SHA256,
        "sweep_query_evidence": _STRING_ARRAY,
        "repair_bundle_interactions": {
            "type": "array",
            "items": _REPAIR_INTERACTION_SCHEMA,
        },
    },
    "required": [
        "prior_finding_id",
        "check_key",
        "changed_section_ids",
        "accepted_resolution",
        "deviation_from_minimal_repair",
        "changed_symbols",
        "consumer_sites_swept",
        "adjacent_variants_swept",
        "validation_evidence",
        "deferred_sites",
    ],
    "additionalProperties": False,
}

REPAIR_ATTESTATIONS_SCHEMA = {
    "type": "array",
    "items": REPAIR_ATTESTATION_SCHEMA,
}

_FAILURE_TRACE_SCHEMA = {
    "type": "object",
    "properties": {
        "preconditions": _NONEMPTY_STRING,
        "action": _NONEMPTY_STRING,
        "wrong_outcome": _NONEMPTY_STRING,
        "violated_obligation": _NONEMPTY_STRING,
        "citation": {
            "type": "array",
            "items": SOURCE_CITATION_SCHEMA,
            "minItems": 1,
        },
    },
    "required": [
        "preconditions",
        "action",
        "wrong_outcome",
        "violated_obligation",
        "citation",
    ],
    "additionalProperties": False,
}

_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "finding_id": _NONEMPTY_STRING,
        "section_id": _NONEMPTY_STRING,
        "check_key": _CHECK_KEY,
        "severity": {
            "type": "string",
            "enum": ["blocking", "major", "minor", "nit"],
        },
        "category": {
            "type": "string",
            "enum": [
                "missing-requirement",
                "bad-sequencing",
                "unhandled-edge",
                "weak-testability",
                "traceability",
                "over-engineering",
                "gobby-format",
            ],
        },
        "location": _NONEMPTY_STRING,
        "description": _NONEMPTY_STRING,
        "minimal_repair": _NONEMPTY_STRING,
        "repair_scope": {
            "type": "string",
            "enum": ["existing_sections", "new_deliverable"],
        },
        "prevention": _NONEMPTY_STRING,
        "principle": _NONEMPTY_STRING,
        "root_cause": _NONEMPTY_STRING,
        "causal_finding_id": _NONEMPTY_STRING,
        "new_deliverable_justification": _NONEMPTY_STRING,
        "participating_section_ids": _STRING_ARRAY,
        "causal_section_ids": _STRING_ARRAY,
        "failure_trace": _FAILURE_TRACE_SCHEMA,
        "introduced_in_round": {"type": "integer", "minimum": 1},
    },
    "required": [
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
    ],
    "anyOf": [
        {"required": ["principle"]},
        {"required": ["root_cause"]},
    ],
    "allOf": [
        {
            "if": {
                "properties": {"severity": {"const": "blocking"}},
                "required": ["severity"],
            },
            "then": {"required": ["failure_trace"]},
        },
        {
            "if": {
                "properties": {"repair_scope": {"const": "new_deliverable"}},
                "required": ["repair_scope"],
            },
            "then": {"required": ["new_deliverable_justification"]},
            "else": {"not": {"required": ["new_deliverable_justification"]}},
        },
    ],
    "dependentRequired": {
        "introduced_in_round": ["causal_finding_id", "causal_section_ids"],
        "causal_finding_id": ["introduced_in_round", "causal_section_ids"],
        "causal_section_ids": ["introduced_in_round", "causal_finding_id"],
    },
    "additionalProperties": False,
}

_MANIFEST_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": _NONEMPTY_STRING,
        "category": _NONEMPTY_STRING,
        "task_type": _NONEMPTY_STRING,
        "depends_on": _STRING_ARRAY,
        "validation_criteria": _NONEMPTY_STRING,
        "labels": _STRING_ARRAY,
        "tdd": {"type": "boolean"},
        "source_section": _NONEMPTY_STRING,
        "implementation_domain": {
            "type": "string",
            "enum": ["frontend", "backend", "fullstack"],
        },
        "assigned_agent": _NONEMPTY_STRING,
    },
    "required": [
        "title",
        "category",
        "task_type",
        "depends_on",
        "validation_criteria",
        "labels",
        "tdd",
        "source_section",
    ],
    "oneOf": [
        {
            "required": ["implementation_domain"],
            "not": {"required": ["assigned_agent"]},
        },
        {
            "required": ["assigned_agent"],
            "not": {"required": ["implementation_domain"]},
        },
    ],
    "additionalProperties": False,
}

_COVERAGE_LANE_SCHEMA = {
    "type": "object",
    "properties": {
        "lane_id": {
            "type": "string",
            "enum": ["requirements", "failure-paths", "integration"],
        },
        "status": {"const": "completed"},
        "candidate_count": {"type": "integer", "minimum": 0},
    },
    "required": ["lane_id", "status", "candidate_count"],
    "additionalProperties": False,
}

_DISPOSITION_COUNTS_SCHEMA = {
    "type": "object",
    "properties": {
        "total": {"type": "integer", "minimum": 0},
        "emitted_findings": {"type": "integer", "minimum": 0},
        "dismissed": {"type": "integer", "minimum": 0},
    },
    "required": ["total", "emitted_findings", "dismissed"],
    "additionalProperties": False,
}

_MANIFEST_DIAGNOSTIC_SCHEMA = {
    "type": "object",
    "properties": {
        "code": _NONEMPTY_STRING,
        "line": {"type": "integer", "minimum": 1},
        "message": _NONEMPTY_STRING,
    },
    "required": ["code", "message"],
    "additionalProperties": False,
}

_SHADOW_MANIFEST_STATUS_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "status": {"const": "valid"},
                "manifest_digest": _SHA256,
                "entry_count": {"type": "integer", "minimum": 1},
            },
            "required": ["status", "manifest_digest", "entry_count"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "status": {"const": "invalid"},
                "diagnostics": {
                    "type": "array",
                    "items": _MANIFEST_DIAGNOSTIC_SCHEMA,
                    "minItems": 1,
                },
            },
            "required": ["status", "diagnostics"],
            "additionalProperties": False,
        },
    ],
}

_COVERAGE_ATTESTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "version": {"const": 1},
        "evidence_id": _NONEMPTY_STRING,
        "lanes": {
            "type": "array",
            "items": _COVERAGE_LANE_SCHEMA,
            "minItems": 3,
            "maxItems": 3,
        },
        "source_digest": _SHA256,
        "disposition_counts": _DISPOSITION_COUNTS_SCHEMA,
        "cross_lane_interaction_complete": {"const": True},
        "adjacent_variant_complete": {"const": True},
        "record_bundle": CANDIDATE_DISPOSITIONS_SCHEMA,
        "shadow_manifest_status": _SHADOW_MANIFEST_STATUS_SCHEMA,
        "attestation_digest": _SHA256,
    },
    "required": [
        "version",
        "evidence_id",
        "lanes",
        "source_digest",
        "disposition_counts",
        "cross_lane_interaction_complete",
        "adjacent_variant_complete",
        "record_bundle",
        "shadow_manifest_status",
        "attestation_digest",
    ],
    "additionalProperties": False,
}

_ATTESTED_ROUND_PROPERTIES = {
    "verdict": {
        "type": "string",
        "enum": ["approved", "needs_review"],
    },
    "findings": {
        "type": "array",
        "items": _FINDING_SCHEMA,
    },
    "coverage_attestation": _COVERAGE_ATTESTATION_SCHEMA,
    "convergence_telemetry": CONVERGENCE_TELEMETRY_SCHEMA,
    "routing_decisions": ROUTING_DECISIONS_SCHEMA,
    "manifest_entries": {
        "type": "array",
        "items": _MANIFEST_ENTRY_SCHEMA,
        "minItems": 1,
    },
}

ROUND_RESULT_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                **_ATTESTED_ROUND_PROPERTIES,
                "verdict": {"const": "approved"},
            },
            "required": [
                "verdict",
                "findings",
                "coverage_attestation",
                "convergence_telemetry",
                "routing_decisions",
                "manifest_entries",
            ],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                **_ATTESTED_ROUND_PROPERTIES,
                "verdict": {"const": "needs_review"},
            },
            "required": [
                "verdict",
                "findings",
                "coverage_attestation",
                "convergence_telemetry",
            ],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "verdict": {"const": "needs_requirements"},
                "evidence_id": _NONEMPTY_STRING,
                "reason": {
                    "type": "object",
                    "properties": {
                        "reason_code": {"const": "missing_requirements"},
                        "questions": {
                            **_STRING_ARRAY,
                            "minItems": 1,
                        },
                    },
                    "required": ["reason_code", "questions"],
                    "additionalProperties": False,
                },
                "convergence_telemetry": CONVERGENCE_TELEMETRY_SCHEMA,
            },
            "required": ["verdict", "evidence_id", "reason", "convergence_telemetry"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "verdict": {"const": "inconclusive"},
                "evidence_id": _NONEMPTY_STRING,
                "reason": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "reason_code": {"const": "source_drift"},
                                "paths": {
                                    **_STRING_ARRAY,
                                    "minItems": 1,
                                },
                            },
                            "required": ["reason_code", "paths"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "reason_code": {"const": "timeout"},
                                "timeout_seconds": {
                                    "type": "number",
                                    "exclusiveMinimum": 0,
                                },
                            },
                            "required": ["reason_code", "timeout_seconds"],
                            "additionalProperties": False,
                        },
                    ],
                },
                "convergence_telemetry": CONVERGENCE_TELEMETRY_SCHEMA,
            },
            "required": ["verdict", "evidence_id", "reason", "convergence_telemetry"],
            "additionalProperties": False,
        },
    ],
}

LESSON_MINT_DETAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "lesson_ids": _STRING_ARRAY,
        "minted_lesson_ids": _STRING_ARRAY,
        "detail": {"type": ["string", "null"]},
    },
    "required": [],
    "additionalProperties": False,
}
