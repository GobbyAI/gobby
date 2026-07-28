"""Round-result union parity across all plan-review producers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from gobby.plans.review_evidence_models import validate_round_result
from tests.review_telemetry_helpers import delivered_telemetry, unavailable_telemetry

ROOT = Path(__file__).resolve().parents[2]
UNION_START = "TERMINAL_RESULT_UNION_V1_START"
UNION_END = "TERMINAL_RESULT_UNION_V1_END"


def _coverage(evidence_id: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "evidence_id": evidence_id,
        "lanes": [
            {
                "lane_id": "requirements_traceability",
                "status": "completed",
                "candidate_count": 0,
            },
            {
                "lane_id": "repository_blast_radius",
                "status": "completed",
                "candidate_count": 0,
            },
            {
                "lane_id": "runtime_invariants",
                "status": "completed",
                "candidate_count": 0,
            },
        ],
        "source_digest": "a" * 64,
        "disposition_counts": {
            "total": 0,
            "emitted_findings": 0,
            "dismissed": 0,
        },
        "cross_lane_interaction_complete": True,
        "adjacent_variant_complete": True,
        "record_bundle": {
            "cross_lane_interactions": [],
            "adjacent_variant_sweeps": [],
            "causal_repair_sweeps": [],
            "candidate_dispositions": [],
        },
        "shadow_manifest_status": {
            "status": "valid",
            "manifest_digest": "b" * 64,
            "entry_count": 1,
        },
    }
    payload["attestation_digest"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def _published_union(path: Path) -> str:
    content = path.read_text()
    assert UNION_START in content, path
    assert UNION_END in content, path
    block = content.split(UNION_START, 1)[1].split(UNION_END, 1)[0]
    return "\n".join(line.strip() for line in block.splitlines() if line.strip())


def test_terminal_branch_union_producer_parity() -> None:
    evidence_id = "evidence-1"
    payloads: list[dict[str, object]] = [
        {
            "verdict": "approved",
            "findings": [],
            "coverage_attestation": _coverage(evidence_id),
            "manifest_entries": [{"artifact_kind": "test"}],
            "routing_decisions": {},
            "convergence_telemetry": delivered_telemetry(),
        },
        {
            "verdict": "needs_review",
            "findings": [],
            "coverage_attestation": _coverage(evidence_id),
            "convergence_telemetry": delivered_telemetry(),
        },
        {
            "verdict": "needs_requirements",
            "evidence_id": evidence_id,
            "reason": {
                "reason_code": "missing_requirements",
                "questions": ["Which service owns the retry deadline?"],
            },
            "convergence_telemetry": delivered_telemetry(),
        },
        {
            "verdict": "inconclusive",
            "evidence_id": evidence_id,
            "reason": {
                "reason_code": "source_drift",
                "paths": ["src/gobby/plans/review_evidence.py"],
            },
            "convergence_telemetry": delivered_telemetry(),
        },
        {
            "verdict": "inconclusive",
            "evidence_id": evidence_id,
            "reason": {
                "reason_code": "timeout",
                "timeout_seconds": 900,
            },
            "convergence_telemetry": unavailable_telemetry(),
        },
    ]

    assert [validate_round_result(payload) for payload in payloads] == payloads

    producer_paths = [
        ROOT / "src/gobby/install/shared/skills/plan-review/SKILL.md",
        ROOT / "src/gobby/install/shared/workflows/agents/plan-adversary.yaml",
        ROOT / "src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml",
    ]
    published = [_published_union(path) for path in producer_paths]
    assert published[1:] == published[:-1]
    for discriminator in (
        '"verdict":"approved"',
        '"verdict":"needs_review"',
        '"verdict":"needs_requirements"',
        '"verdict":"inconclusive"',
        '"reason_code":"source_drift"',
        '"reason_code":"missing_requirements"',
        '"reason_code":"timeout"',
    ):
        assert discriminator in published[0]
