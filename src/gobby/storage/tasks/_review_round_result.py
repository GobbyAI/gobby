"""Canonical staged plan-review result construction."""

from __future__ import annotations

from gobby.plans.review_evidence_models import validate_round_result


def build_approved_round_result(
    *,
    findings: list[dict[str, object]],
    manifest_entries: list[dict[str, object]],
    routing_decisions: dict[str, object],
    coverage_attestation: dict[str, object],
    convergence_telemetry: dict[str, object],
) -> dict[str, object]:
    """Build and validate an approved staged review result."""
    return validate_round_result(
        {
            "verdict": "approved",
            "findings": findings,
            "manifest_entries": manifest_entries,
            "routing_decisions": routing_decisions,
            "coverage_attestation": coverage_attestation,
            "convergence_telemetry": convergence_telemetry,
        }
    )


def build_rejected_round_result(
    *,
    findings: list[dict[str, object]],
    coverage_attestation: dict[str, object],
    convergence_telemetry: dict[str, object],
) -> dict[str, object]:
    """Build and validate a rejected staged review result."""
    return validate_round_result(
        {
            "verdict": "needs_review",
            "findings": findings,
            "coverage_attestation": coverage_attestation,
            "convergence_telemetry": convergence_telemetry,
        }
    )
