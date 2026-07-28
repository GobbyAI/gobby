"""Canonical convergence-telemetry fixtures shared by review tests."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

from gobby.plans.review_telemetry import derive_daemon_aggregates, enrich_round_result


def delivered_telemetry() -> dict[str, object]:
    classification = {
        "check_key": "terminal-path-totality",
        "check_key_class": "terminal-path",
        "finding_ids": ["finding-7"],
        "ledger_ids": ["ledger-2"],
        "classification_inputs": [
            {
                "name": "terminal_routes",
                "value": "session_end,workflow,kill,cancel",
            }
        ],
    }
    provenance = {
        "finding_ids": ["finding-7"],
        "ledger_ids": ["ledger-2"],
        "classification_inputs": [
            {
                "name": "changed_sections",
                "value": "7.2",
            }
        ],
    }
    return {
        "state": "delivered",
        "reviewer": {
            "status": "available",
            "reviewer_miss": {
                "count": 1,
                "classifications": [classification],
            },
            "fixer_induced": {
                "count": 0,
                "classifications": [],
            },
            "repeated_check_keys": {
                "count": 1,
                "classifications": [classification],
            },
            "remedy_scope": {
                "scope": "cross_section",
                **provenance,
            },
            "ledger_entries_carried": {
                "count": 1,
                **provenance,
            },
            "artifact_growth": {
                "section_delta": 1,
                "target_delta": 2,
                "acceptance_delta": 3,
                **provenance,
            },
        },
    }


def unavailable_telemetry() -> dict[str, object]:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    run = SimpleNamespace(
        created_at=started_at,
        completed_at=started_at + timedelta(seconds=1),
        tool_calls_count=0,
        turns_used=0,
    )
    return {
        "state": "enriched",
        "reviewer": {
            "status": "unavailable",
            "reason": "reviewer_result_not_delivered",
        },
        "daemon": derive_daemon_aggregates(
            run,
            terminal_status="timeout",
            finding_count=0,
        ),
    }


def enriched_telemetry() -> dict[str, object]:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    result = enrich_round_result(
        {
            "verdict": "needs_review",
            "findings": [],
            "convergence_telemetry": delivered_telemetry(),
        },
        run=SimpleNamespace(
            created_at=started_at,
            completed_at=started_at + timedelta(seconds=1),
            tool_calls_count=0,
            turns_used=0,
        ),
        terminal_status="success",
    )
    return cast(dict[str, object], result["convergence_telemetry"])
