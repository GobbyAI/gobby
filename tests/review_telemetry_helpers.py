"""Canonical convergence-telemetry fixtures shared by review tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_requirements import REQUEST_ANCHOR_VARIABLE, build_request_anchor
from gobby.plans.review_telemetry import derive_daemon_aggregates, enrich_round_result
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager


@dataclass(frozen=True)
class BoundReview:
    evidence_id: str
    run_id: str
    parent_session_id: str
    child_session_id: str


def bound_review(
    temp_db: HubDatabase,
    tmp_path: Path,
    *,
    suffix: str = "",
) -> BoundReview:
    project = LocalProjectManager(temp_db).create(
        name=f"terminal-review{suffix}",
        repo_path=str(tmp_path),
    )
    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id=f"terminal-parent{suffix}",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    child = sessions.register(
        external_id=f"terminal-child{suffix}",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
        parent_session_id=parent.id,
        agent_depth=1,
    )
    plan_path = tmp_path / f"terminal-review{suffix}.md"
    plan_path.write_text(
        "\n".join(
            [
                "# Terminal Review",
                "**Plan ID:** terminal-review",
                "",
                "## P1 Phase",
                "`kind: framing`",
                "",
                "### 1.1 Work",
                "`kind: deliverable`",
                "",
                "Target: `src/example.py`",
                "",
                "**Acceptance:**",
                "- 1.1.1 — Exists. test: `tests/test_example.py`",
                "",
                "## Task Mapping",
                "`kind: framing`",
                "",
                "Pending.",
                "",
                "## V1 Plan Changelog",
                "`kind: verification`",
                "",
                "No rounds.",
                "",
                "## M1 Task Manifest",
                "`kind: manifest`",
                "",
                "```yaml",
                "[]",
                "```",
            ]
        ),
        encoding="utf-8",
    )
    SessionVariableManager(temp_db).merge_variables(
        parent.id,
        {
            REQUEST_ANCHOR_VARIABLE: build_request_anchor(
                f"terminal-review-request{suffix}",
                "Review the terminal plan",
            )
        },
    )
    evidence_service = PlanReviewEvidenceService(temp_db)
    prepared = evidence_service.prepare_plan_review_round(
        project_id=project.id,
        plan_path=plan_path,
        round_number=1,
        session_id=parent.id,
    )
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        provider="codex",
        prompt="Review the plan.",
    )
    evidence_service.bind_evidence_run(prepared.evidence_id, run.id)
    return BoundReview(
        evidence_id=prepared.evidence_id,
        run_id=run.id,
        parent_session_id=parent.id,
        child_session_id=child.id,
    )


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
                "classifications": [deepcopy(classification)],
            },
            "fixer_induced": {
                "count": 0,
                "classifications": [],
            },
            "repeated_check_keys": {
                "count": 1,
                "classifications": [deepcopy(classification)],
            },
            "remedy_scope": {
                "scope": "cross_section",
                **deepcopy(provenance),
            },
            "ledger_entries_carried": {
                "count": 1,
                **deepcopy(provenance),
            },
            "artifact_growth": {
                "section_delta": 1,
                "target_delta": 2,
                "acceptance_delta": 3,
                **deepcopy(provenance),
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
