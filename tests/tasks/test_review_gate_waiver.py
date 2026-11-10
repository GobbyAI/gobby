"""Review gates refuse a runbook-like caller and waive a caller holding the "*" wildcard.

Bundled rules are enumerated generically: every closure or landing rule whose condition
tests the waiver capability is evaluated against synthetic callers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import yaml

from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.tasks.attestation_authorship import attestation_authorship_refusal
from gobby.tasks.attestation_shapes import CLOSURE_EVENTS
from gobby.tasks.attestations import TaskAttestation
from gobby.tasks.closure_policy import build_closure_condition_helpers
from gobby.workflows.agent_models import ALL_CAPABILITIES, WAIVE_REVIEW_GATES, AgentDefinitionBody
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.sync_rules import get_bundled_rules_path

pytestmark = pytest.mark.unit

_AT = datetime(2026, 11, 10, tzinfo=UTC)
_RUNBOOK_CAPABILITIES = ["land_tasks", "coordinate_tasks"]
_SYNTHETIC_TAG = "synthetic-review-gates"


def _bundled_closure_rules() -> list[tuple[str, dict[str, Any]]]:
    rules: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(get_bundled_rules_path().rglob("*.yaml")):
        if {"deprecated", "_deprecated"}.intersection(path.parts):
            continue
        document = yaml.safe_load(path.read_text()) or {}
        for name, definition in (document.get("rules") or {}).items():
            if definition.get("event") in CLOSURE_EVENTS:
                rules.append((name, definition))
    return rules


_WAIVED = [
    pytest.param(definition["when"], id=name)
    for name, definition in _bundled_closure_rules()
    if WAIVE_REVIEW_GATES in str(definition.get("when", ""))
]


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
    """A closure snapshot that trips every review gate: the implementer reviewed its own work."""
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


def _blocks(when: str, capabilities: list[str]) -> bool:
    context = _context(capabilities)
    evaluator = SafeExpressionEvaluator(context, build_closure_condition_helpers(context))
    return bool(evaluator.evaluate(when))


def test_bundled_review_gates_declare_the_waiver() -> None:
    assert len(_WAIVED) >= 2


@pytest.mark.parametrize("when", _WAIVED)
def test_runbook_caller_is_still_refused(when: str) -> None:
    assert _blocks(when, _RUNBOOK_CAPABILITIES)


@pytest.mark.parametrize("when", _WAIVED)
def test_wildcard_caller_needs_no_review(when: str) -> None:
    assert not _blocks(when, [ALL_CAPABILITIES])


def _authorship_rows() -> list[RuleDefinitionRow]:
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
            tags=[_SYNTHETIC_TAG],
            created_at=_AT,
            updated_at=_AT,
        )
        for name, definition in _bundled_closure_rules()
        if definition.get("attestation_authorship")
    ]


def _author(capabilities: list[str]) -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(
        {
            "name": "synthetic-author",
            "prompts": {"agent": "Act."},
            "capabilities": capabilities,
            "workflows": {"rule_selectors": {"include": [f"tag:{_SYNTHETIC_TAG}"]}},
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
    rows = _authorship_rows()
    assert rows
    refusal = attestation_authorship_refusal(
        kind="independent-review",
        session_id="implementer",
        records=[_implementation()],
        reference=None,
        agent=_author(capabilities),
        rules=rows,
    )
    assert (refusal is not None) is refused
