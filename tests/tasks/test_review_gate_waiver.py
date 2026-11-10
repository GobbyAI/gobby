"""Review gates refuse a runbook-like caller and waive a caller holding the "*" wildcard."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.tasks.attestation_authorship import attestation_authorship_refusal
from gobby.tasks.attestations import TaskAttestation
from gobby.tasks.closure_policy import build_closure_condition_helpers
from gobby.workflows.agent_models import ALL_CAPABILITIES, AgentDefinitionBody
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.sync_rules import get_bundled_rules_path

pytestmark = pytest.mark.unit

_AT = datetime(2026, 11, 10, tzinfo=UTC)
_RUNBOOK_CAPABILITIES = ["land_tasks", "coordinate_tasks"]
_GATES = [
    ("task-closure/implementer-review.yaml", "closure-review-by-implementer-refused"),
    ("task-closure/implementer-review.yaml", "landing-review-by-implementer-refused"),
    ("task-closure/landing.yaml", "landing-requires-path-grant-or-independent-review"),
    ("task-closure/epic.yaml", "epic-completion-requires-descendants"),
    ("task-closure/epic.yaml", "epic-landing-requires-descendants"),
    ("task-closure/epic.yaml", "epic-completion-requires-exact-evidence"),
    ("task-closure/epic.yaml", "epic-landing-requires-exact-evidence"),
    ("task-closure/epic.yaml", "epic-completion-requires-independent-author"),
    ("task-closure/epic.yaml", "epic-landing-requires-independent-author"),
    ("task-closure/security.yaml", "closure-requires-security-approval"),
    ("task-closure/security.yaml", "landing-requires-security-approval"),
]


def _rule(relative: str, name: str) -> dict[str, Any]:
    document = yaml.safe_load(Path(get_bundled_rules_path(), relative).read_text())
    rule: dict[str, Any] = document["rules"][name]
    return rule


def _record(kind: str, sequence: int) -> dict[str, Any]:
    return {
        "id": f"{kind}-{sequence}",
        "kind": kind,
        "result": "approved",
        "session_id": "implementer",
        "reference": None,
        "supersedes_id": None,
        "sequence": sequence,
        "evidence": {},
        "agent_capabilities": _RUNBOOK_CAPABILITIES,
    }


def _context(capabilities: list[str]) -> dict[str, Any]:
    records = [_record("implementation", 1), _record("independent-review", 2)]
    return {
        "reason": "done",
        "task": {
            "labels": ["epic-devteam", "security-review"],
            "category": "code",
            "claimed_by_session_id": "implementer",
        },
        "caller": {"session_id": "implementer", "capabilities": capabilities},
        "facts": {
            "closure_intent": "explicit",
            "integration_path_grant": False,
            "descendants_closed": False,
            "descendants": [],
            "landing_receipts": [],
            "claimants": [],
        },
        "attestations": records,
        "all_attestations": records,
    }


def _blocks(relative: str, name: str, capabilities: list[str]) -> bool:
    context = _context(capabilities)
    evaluator = SafeExpressionEvaluator(context, build_closure_condition_helpers(context))
    return bool(evaluator.evaluate(_rule(relative, name)["when"]))


@pytest.mark.parametrize(("relative", "name"), _GATES)
def test_runbook_caller_is_still_refused(relative: str, name: str) -> None:
    assert _blocks(relative, name, _RUNBOOK_CAPABILITIES)


@pytest.mark.parametrize(("relative", "name"), _GATES)
def test_wildcard_caller_needs_no_review(relative: str, name: str) -> None:
    assert not _blocks(relative, name, [ALL_CAPABILITIES])


def _implementer_review_rows() -> list[RuleDefinitionRow]:
    document = yaml.safe_load(
        Path(get_bundled_rules_path(), "task-closure/implementer-review.yaml").read_text()
    )
    return [
        RuleDefinitionRow(
            id=name,
            name=name,
            enabled=True,
            enabled_pinned=False,
            priority=int(definition.get("priority", 100)),
            definition_json={
                key: value
                for key, value in definition.items()
                if key not in {"description", "priority"}
            },
            source="installed",
            tags=["synthetic-review-gates"],
            created_at=_AT,
            updated_at=_AT,
        )
        for name, definition in document["rules"].items()
    ]


def _author(capabilities: list[str]) -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(
        {
            "name": "synthetic-author",
            "prompts": {"agent": "Act."},
            "capabilities": capabilities,
            "workflows": {"rule_selectors": {"include": ["tag:synthetic-review-gates"]}},
        }
    )


def _implementation() -> TaskAttestation:
    return TaskAttestation(
        id="implementation-1",
        task_id="task",
        sequence=1,
        session_id="implementer",
        agent_definition="synthetic-author",
        agent_definition_hash="hash",
        agent_capabilities=(),
        created_at=_AT,
        kind="implementation",
        result="approved",
        summary="Implemented.",
    )


@pytest.mark.parametrize(
    ("capabilities", "refused"), [(_RUNBOOK_CAPABILITIES, True), ([ALL_CAPABILITIES], False)]
)
def test_implementer_may_record_review_only_with_the_wildcard(
    capabilities: list[str], refused: bool
) -> None:
    refusal = attestation_authorship_refusal(
        kind="independent-review",
        session_id="implementer",
        records=[_implementation()],
        reference=None,
        agent=_author(capabilities),
        rules=_implementer_review_rows(),
    )
    assert (refusal is not None) is refused
