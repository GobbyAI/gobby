"""Record time refuses evidence that a selected closure rule's declared shape can never accept."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.attestations import (
    AttestationError,
    AttestationSubmission,
    EvidenceReference,
    list_attestations,
    record_attestation,
)
from gobby.workflows.definitions import RuleDefinitionBody
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit

_RULES = Path(__file__).parents[2] / "src/gobby/install/shared/workflows/rules"
_SHA = "0123456789abcdef0123456789abcdef01234567"
_SHAPED_KINDS = ["bake", "frost"]


def _shaped_rules(db: HubDatabase, project_id: str) -> None:
    rules = RuleDefinitionManager(db)
    rules.create(
        "bake-shape-result",
        {
            "event": "task_land",
            "when": "any(latest_attestation(k).get('result') != 'tasty' for k in ['taste', 'bake'])",
            "attestation_shape": {"kinds": ["taste", "bake"], "result": "tasty"},
            "effects": [{"type": "block", "reason": "Record tasty evidence."}],
        },
        project_id=project_id,
        tags=["bakery-land"],
    )
    rules.create(
        "bake-shape-reference",
        {
            "event": "task_land",
            "when": "len(attestations) < 0",
            "attestation_shape": {
                "kinds": _SHAPED_KINDS,
                "reference": {"scheme": "oven", "value_pattern": "[0-9a-f]{40}"},
                "applies_when": "task.get('category') != 'manual'",
            },
            "effects": [{"type": "block", "reason": "Name the full oven batch."}],
        },
        project_id=project_id,
        tags=["bakery-land"],
    )


def _author(
    db: HubDatabase, project_id: str, *, selected: bool = True, kinds: dict[str, Any] | None = None
) -> str:
    _shaped_rules(db, project_id)
    include = ["tag:bakery-land"] if selected else []
    return AgentSessions(db, project_id).add(
        "baker-selected" if selected else "baker-unselected",
        ["*"],
        definition_fields={
            "workflows": {"rule_selectors": {"include": include}},
            "task_attestation_kinds": kinds or {},
        },
    )


def _submit(
    db: HubDatabase,
    author: str,
    task_id: str,
    kind: str,
    result: str,
    ref: dict[str, str] | None = None,
) -> None:
    record_attestation(
        db,
        task_id=task_id,
        session_id=author,
        submission=AttestationSubmission(
            kind=kind,
            result=result,
            summary="Observed.",
            reference=EvidenceReference.model_validate(ref) if ref else None,
        ),
    )


@pytest.mark.parametrize(
    ("result", "reference", "problem"),
    [
        ("pass", {"scheme": "oven", "value": _SHA}, "result 'pass'"),
        ("tasty", None, "no reference"),
        ("tasty", {"scheme": "kiln", "value": _SHA}, "reference scheme 'kiln'"),
        ("tasty", {"scheme": "oven", "value": _SHA[:10]}, "reference value"),
        ("tasty", {"scheme": "oven", "value": _SHA.upper()}, "reference value"),
    ],
)
def test_unacceptable_shape_is_refused_with_the_accepted_shape(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    result: str,
    reference: dict[str, str] | None,
    problem: str,
) -> None:
    author = _author(temp_db, sample_project["id"])
    task = LocalTaskManager(temp_db).create_task(sample_project["id"], "Bake a cake")
    with pytest.raises(AttestationError) as refused:
        _submit(temp_db, author, task.id, "bake", result, reference)
    message = str(refused.value)
    assert problem in message
    assert "result 'tasty'" in message
    assert "scheme 'oven'" in message and "'[0-9a-f]{40}'" in message
    assert list_attestations(temp_db, task.id) == []


def test_acceptable_and_uncovered_shapes_are_recorded(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    author = _author(temp_db, sample_project["id"])
    task = LocalTaskManager(temp_db).create_task(sample_project["id"], "Bake a cake")
    _submit(temp_db, author, task.id, "bake", "tasty", {"scheme": "oven", "value": _SHA})
    _submit(temp_db, author, task.id, "taste", "tasty")
    _submit(temp_db, author, task.id, "plate", "pretty")
    _submit(temp_db, author, task.id, "frost", "anything", {"scheme": "oven", "value": _SHA})
    assert [r.kind for r in list_attestations(temp_db, task.id)] == [
        "bake",
        "taste",
        "plate",
        "frost",
    ]


def test_declared_negative_result_stays_recordable(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    kinds = {
        kind: {"allowed_results": ["tasty", "burnt"], "guidance": "Report the bake."}
        for kind in ["taste", *_SHAPED_KINDS]
    }
    author = _author(temp_db, sample_project["id"], kinds=kinds)
    task = LocalTaskManager(temp_db).create_task(sample_project["id"], "Bake a cake")
    _submit(temp_db, author, task.id, "bake", "burnt")
    with pytest.raises(AttestationError, match="no reference"):
        _submit(temp_db, author, task.id, "bake", "tasty")
    assert [r.result for r in list_attestations(temp_db, task.id)] == ["burnt"]


def test_shapes_apply_only_to_tasks_their_expression_selects(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    author = _author(temp_db, sample_project["id"])
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"], "Serve raw dough", category="manual"
    )
    _submit(temp_db, author, task.id, "frost", "anything", {"scheme": "kiln", "value": "batch"})
    with pytest.raises(AttestationError, match="result 'pass'"):
        _submit(temp_db, author, task.id, "bake", "pass")
    assert [r.kind for r in list_attestations(temp_db, task.id)] == ["frost"]


def test_unselected_rule_shapes_do_not_constrain_recording(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    author = _author(temp_db, sample_project["id"], selected=False)
    task = LocalTaskManager(temp_db).create_task(sample_project["id"], "Bake a cake")
    _submit(temp_db, author, task.id, "bake", "pass")
    assert [r.result for r in list_attestations(temp_db, task.id)] == ["pass"]


@pytest.mark.parametrize(
    ("event", "shape", "message"),
    [
        ("before_tool", {"kinds": ["bake"], "result": "tasty"}, "attestation_shape"),
        ("task_land", {"kinds": ["bake"]}, "result or reference"),
        ("task_land", {"kinds": [], "result": "tasty"}, "kinds"),
        (
            "task_land",
            {"kinds": ["bake"], "reference": {"scheme": "oven", "value_pattern": "("}},
            "value_pattern",
        ),
        (
            "task_land",
            {"kinds": ["bake"], "result": "tasty", "applies_when": "open('oven')"},
            "applies_when",
        ),
    ],
)
def test_attestation_shape_schema_is_closed(
    event: str, shape: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        RuleDefinitionBody.model_validate(
            {
                "event": event,
                "attestation_shape": shape,
                "effects": [{"type": "block", "reason": "No."}],
            }
        )


def _bundled_shaped_rules() -> list[tuple[str, dict[str, Any]]]:
    shaped = []
    for path in sorted(_RULES.rglob("*.yaml")):
        document = yaml.safe_load(path.read_text()) or {}
        for name, rule in (document.get("rules") or {}).items():
            if isinstance(rule, dict) and "attestation_shape" in rule:
                shaped.append((name, rule))
    return shaped


def test_bundled_shapes_name_what_their_rule_expressions_check() -> None:
    shaped = _bundled_shaped_rules()
    assert shaped
    for name, rule in shaped:
        body = RuleDefinitionBody.model_validate(
            {key: value for key, value in rule.items() if key not in {"description", "priority"}}
        )
        shape = body.attestation_shape
        assert shape is not None and body.when is not None, name
        expected = [*shape.kinds]
        if shape.result is not None:
            expected.append(shape.result)
        if shape.reference is not None:
            expected.append(shape.reference.scheme)
        for literal in expected:
            assert f"'{literal}'" in body.when, (name, literal)
        if shape.applies_when is not None:
            assert " ".join(shape.applies_when.split()) in " ".join(body.when.split()), name
