"""Bundled definitions honor the bundled authorship rule, enumerated generically.

The rule's authorship split is read from the bundled YAML. Nothing here names a bundled
definition, rule or pipeline except the exempt `default` definition.
"""

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml

from gobby.agents.team_admission_records import TeamBinding
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.attestations import (
    AttestationError,
    AttestationSubmission,
    list_attestations,
    record_attestation,
)
from gobby.tasks.closure_policy import ClosureDecision, ClosureSnapshot, evaluate_closure_policy
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.pipeline_state import StepStatus
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit

_WORKFLOWS = Path(__file__).parents[2] / "src/gobby/install/shared/workflows"
_EXEMPT = "default"
# Row-level keys sync stores beside a rule body, not inside it.
_ROW_KEYS = {"description", "priority"}


def _yaml_files(subdir: str) -> list[dict[str, Any]]:
    return [
        yaml.safe_load(path.read_text()) for path in sorted((_WORKFLOWS / subdir).rglob("*.yaml"))
    ]


def _definitions() -> list[dict[str, Any]]:
    return _yaml_files("agents")


def _declares_authorship(body: Any) -> bool:
    return isinstance(body, dict) and "attestation_authorship" in body


def _authorship_documents() -> list[dict[str, Any]]:
    return [
        document
        for document in _yaml_files("rules")
        if any(_declares_authorship(body) for body in (document.get("rules") or {}).values())
    ]


def _authorship_rules() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (document, body)
        for document in _authorship_documents()
        for body in (document.get("rules") or {}).values()
        if _declares_authorship(body)
    ]


def _authorship_selectors() -> list[str]:
    tags = [tag for document in _authorship_documents() for tag in document.get("tags") or []]
    return [f"tag:{tag}" for tag in sorted(set(tags))]


def _declared_authorship() -> tuple[str, set[str]]:
    declared = _authorship_rules()
    assert declared, "no bundled rule declares attestation_authorship"
    authorships = {
        (
            body["attestation_authorship"]["implementation_kind"],
            tuple(body["attestation_authorship"]["maker_kinds"]),
        )
        for _document, body in declared
    }
    assert len(authorships) == 1, "bundled rules disagree on the authorship split"
    implementation_kind, makers = next(iter(authorships))
    return implementation_kind, set(makers)


def _selects_tag(definition: dict[str, Any]) -> bool:
    include = (definition.get("workflows") or {}).get("rule_selectors", {}).get("include", [])
    return any(selector in include for selector in _authorship_selectors())


def test_bundled_authorship_declarations_are_valid_closure_rules() -> None:
    assert _authorship_rules(), "no bundled rule declares attestation_authorship"
    for _document, body in _authorship_rules():
        validated = RuleDefinitionBody.model_validate(
            {key: value for key, value in body.items() if key not in _ROW_KEYS}
        )
        assert validated.attestation_authorship is not None
        assert validated.event.value in {"task_close", "task_land"}


def test_default_definition_is_exempt_by_not_selecting_the_rule() -> None:
    exempt = [d for d in _definitions() if d.get("name") == _EXEMPT]
    assert len(exempt) == 1
    assert not _selects_tag(exempt[0])


def test_every_non_default_definition_that_writes_attestations_selects_the_rule() -> None:
    missing = [
        definition["name"]
        for definition in _definitions()
        if definition.get("name") != _EXEMPT
        and definition.get("task_attestation_writes")
        and not _selects_tag(definition)
    ]
    assert missing == []


def test_explicit_writes_never_pair_the_implementation_kind_with_a_review_kind() -> None:
    implementation_kind, makers = _declared_authorship()
    offenders = [
        definition["name"]
        for definition in _definitions()
        if definition.get("name") != _EXEMPT
        and implementation_kind in (definition.get("task_attestation_writes") or [])
        and not set(definition["task_attestation_writes"]) <= makers
    ]
    assert offenders == []


def _claim_flows() -> list[dict[str, Any]]:
    flows: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "claim_flow" and isinstance(value, dict):
                    flows.append(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for document in _yaml_files("pipelines"):
        walk(document)
    return flows


def test_claim_flow_stages_that_need_an_implementation_need_only_maker_kinds() -> None:
    implementation_kind, makers = _declared_authorship()
    implement_stages = [
        stage["done_when"]
        for flow in _claim_flows()
        for stage in flow.values()
        if isinstance(stage, dict) and implementation_kind in (stage.get("done_when") or [])
    ]
    assert implement_stages, "no claim_flow stage requires an implementation"
    for done_when in implement_stages:
        assert set(done_when) <= makers


def _bundled_definition(name: str) -> dict[str, Any]:
    matches = [definition for definition in _definitions() if definition.get("name") == name]
    assert len(matches) == 1, f"expected one bundled agent definition named {name!r}"
    return matches[0]


def _implementation_stages() -> list[tuple[dict[str, Any], str]]:
    implementation_kind, _makers = _declared_authorship()
    return [
        (document, name)
        for document in _yaml_files("pipelines")
        for name, stage in (document.get("claim_flow") or {}).items()
        if implementation_kind in (stage.get("done_when") or []) and stage.get("seat")
    ]


def _admits(stage: dict[str, Any], category: str | None) -> bool:
    return not stage.get("categories") or category in stage["categories"]


def test_bundled_claim_flow_declares_a_seat_stage_for_an_implementation() -> None:
    assert _implementation_stages(), "no bundled claim_flow seat stage requires an implementation"


def _legacy_review(db: HubDatabase, seats: AgentSessions, task_id: str) -> str:
    """A session that recorded a review before the authorship rule existed."""
    legacy = seats.add("legacy-seat", ["sign-off"])
    record_attestation(
        db,
        task_id=task_id,
        session_id=legacy,
        submission=AttestationSubmission(kind="sign-off", result="approved", summary="Reviewed."),
    )
    return legacy


@pytest.mark.parametrize("stale_review", [False, True], ids=["fresh", "stale-review"])
@pytest.mark.parametrize("index", range(len(_implementation_stages())))
def test_bundled_implementation_stage_advances_on_its_maker_kinds_alone(
    temp_db: HubDatabase, sample_project: dict[str, Any], index: int, stale_review: bool
) -> None:
    document, stage_name = _implementation_stages()[index]
    project = sample_project["id"]
    _install_bundled_closure_rules(temp_db, project)
    flow: dict[str, Any] = document["claim_flow"]
    stage = flow[stage_name]
    implementation_kind, _makers = _declared_authorship()
    done_when = list(stage["done_when"])
    implementation_index = done_when.index(implementation_kind)
    candidate = {"scheme": "git", "value": "a" * 40}
    steps_by_id = {step["id"]: step for step in document.get("steps") or []}
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project, "Build the change", category="code", validation_criteria="Criteria hold."
    )

    seats = AgentSessions(temp_db, project)
    legacy = _legacy_review(temp_db, seats, task.id) if stale_review else None
    agent_by_seat: dict[str, str] = {}
    session_by_seat: dict[str, str] = {}
    for seat in sorted({entry["seat"] for entry in flow.values() if entry.get("seat")}):
        agent = steps_by_id[seat]["mcp"]["arguments"]["agent"]
        agent_by_seat[seat] = agent
        bundled = _bundled_definition(agent)
        session_by_seat[seat] = seats.add(
            agent,
            definition_fields={
                "task_attestation_writes": bundled.get("task_attestation_writes") or [],
                "task_attestation_kinds": bundled.get("task_attestation_kinds") or {},
            },
        )

    executions = LocalPipelineExecutionManager(temp_db, project)
    execution = executions.create_execution(
        "claim-flow",
        definition_json=json.dumps(
            {
                "name": "claim-flow",
                "claim_flow": flow,
                "steps": [
                    {"id": "admit", "exec": "true"},
                    *[
                        {
                            "id": seat,
                            "mcp": {
                                "server": "seats",
                                "tool": "spawn",
                                "arguments": {"agent": agent},
                            },
                        }
                        for seat, agent in agent_by_seat.items()
                    ],
                ],
            }
        ),
    )
    parent = SessionManager(temp_db).register(
        external_id=f"pipeline-{execution.id}",
        source="pipeline",
        machine_id=require_machine_id(),
        project_id=project,
    )
    binding = TeamBinding(execution.id, task.id, project, "claim-flow", "main", None, None)
    outputs: list[tuple[str, dict[str, Any]]] = [("admit", {"team": asdict(binding)})]
    outputs += [(seat, {"child_session_id": sid}) for seat, sid in session_by_seat.items()]
    for name, output in outputs:
        step = executions.create_step_execution(execution.id, name)
        executions.update_step_execution(
            step.id, status=StepStatus.COMPLETED, output_json=json.dumps(output)
        )
    for session in session_by_seat.values():
        temp_db.execute(
            "UPDATE sessions SET parent_session_id = %s WHERE id = %s", (parent.id, session)
        )
    holder = session_by_seat[stage["seat"]]
    tasks.claim_task(task.id, holder)

    route = next(name for name in stage["next"] if _admits(flow[name], task.category))
    expected = session_by_seat[flow[route]["seat"]]
    for position, kind in enumerate(done_when):
        record_attestation(
            temp_db,
            task_id=task.id,
            session_id=holder,
            submission=AttestationSubmission(
                kind=kind,
                result="approved",
                summary="Observed.",
                reference=candidate if position >= implementation_index else None,
            ),
        )
        if position < len(done_when) - 1:
            assert tasks.get_task(task.id).claimed_by_session_id == holder, kind
    assert tasks.get_task(task.id).claimed_by_session_id == expected
    if legacy is not None:
        assert [
            record.kind
            for record in list_attestations(temp_db, task.id)
            if record.session_id == legacy
        ] == ["sign-off"]


def _install_rules(db: HubDatabase, project_id: str, documents: list[dict[str, Any]]) -> None:
    """Store rule documents the way sync does: row keys beside the body."""
    manager = RuleDefinitionManager(db)
    for document in documents:
        for name, body in (document.get("rules") or {}).items():
            manager.create(
                name,
                {key: value for key, value in body.items() if key not in _ROW_KEYS},
                project_id=project_id,
                description=body.get("description"),
                priority=body.get("priority", 100),
                tags=document["tags"],
            )


def _install_bundled_closure_rules(db: HubDatabase, project_id: str) -> None:
    _install_rules(db, project_id, _authorship_documents())


def _closing_definition() -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(
        {
            "name": "synthetic-closer",
            "prompts": {"agent": "Close the task."},
            "workflows": {"rule_selectors": {"include": _authorship_selectors()}},
        }
    )


def _submit(db: HubDatabase, author: str, task_id: str, kind: str) -> None:
    record_attestation(
        db,
        task_id=task_id,
        session_id=author,
        submission=AttestationSubmission(kind=kind, result="approved", summary="Observed."),
    )


def _snapshot(
    db: HubDatabase, task_id: str, event: Literal["task_close", "task_land"]
) -> ClosureSnapshot:
    return ClosureSnapshot(
        event=event, task={"id": task_id}, attestations=tuple(list_attestations(db, task_id))
    )


@pytest.mark.parametrize("event", ["task_close", "task_land"])
def test_bundled_closure_gate_blocks_an_implementers_review_and_passes_an_independent_one(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    event: Literal["task_close", "task_land"],
) -> None:
    project_id = sample_project["id"]
    _install_bundled_closure_rules(temp_db, project_id)
    implementation_kind, _makers = _declared_authorship()
    seats = AgentSessions(temp_db, project_id)
    # The implementer's definition does not select the rule, so its own review records
    # predate enforcement, as they would for a task recorded before the rule existed.
    implementer = seats.add(
        "synthetic-implementer", definition_fields={"task_attestation_writes": ["*"]}
    )
    reviewer = seats.add(
        "synthetic-reviewer",
        definition_fields={
            "task_attestation_writes": ["*"],
            "workflows": {"rule_selectors": {"include": _authorship_selectors()}},
        },
    )
    task = LocalTaskManager(temp_db).create_task(project_id, "Bake a cake")
    _submit(temp_db, implementer, task.id, implementation_kind)
    _submit(temp_db, implementer, task.id, "sign-off")
    rules = RuleDefinitionManager(temp_db).list_effective(project_id)

    blocked = evaluate_closure_policy(
        _snapshot(temp_db, task.id, event), agent=_closing_definition(), rules=rules
    )
    assert not blocked.allowed
    assert any("independent seat" in reason for reason in blocked.blockers)

    _submit(temp_db, reviewer, task.id, "sign-off")
    decision = evaluate_closure_policy(
        _snapshot(temp_db, task.id, event), agent=_closing_definition(), rules=rules
    )
    assert decision.allowed


@pytest.mark.parametrize("event", ["task_close", "task_land"])
def test_legacy_review_never_blocks_closure_or_landing(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    event: Literal["task_close", "task_land"],
) -> None:
    project_id = sample_project["id"]
    _install_bundled_closure_rules(temp_db, project_id)
    implementation_kind, _makers = _declared_authorship()
    legacy_kind = _legacy_kind()
    seats = AgentSessions(temp_db, project_id)
    # Recorded before enforcement: this implementer's definition does not select the rule.
    implementer = seats.add(
        "synthetic-implementer", definition_fields={"task_attestation_writes": ["*"]}
    )
    reviewer = seats.add(
        "synthetic-reviewer",
        definition_fields={
            "task_attestation_writes": ["*"],
            "workflows": {"rule_selectors": {"include": _authorship_selectors()}},
        },
    )
    task = LocalTaskManager(temp_db).create_task(project_id, "Bake a cake")
    _submit(temp_db, implementer, task.id, implementation_kind)
    _submit(temp_db, implementer, task.id, legacy_kind)
    _submit(temp_db, reviewer, task.id, "sign-off")
    rules = RuleDefinitionManager(temp_db).list_effective(project_id)

    decision = evaluate_closure_policy(
        _snapshot(temp_db, task.id, event), agent=_closing_definition(), rules=rules
    )

    assert decision.allowed, decision.blockers


def test_selecting_implementer_is_refused_a_new_review_of_its_work(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    _install_bundled_closure_rules(temp_db, project_id)
    implementation_kind, _makers = _declared_authorship()
    seats = AgentSessions(temp_db, project_id)
    implementer = seats.add(
        "synthetic-implementer",
        definition_fields={
            "task_attestation_writes": ["*"],
            "workflows": {"rule_selectors": {"include": _authorship_selectors()}},
        },
    )
    task = LocalTaskManager(temp_db).create_task(project_id, "Bake a cake")
    _submit(temp_db, implementer, task.id, implementation_kind)

    legacy_kind = _legacy_kind()
    with pytest.raises(AttestationError, match="independent seat"):
        _submit(temp_db, implementer, task.id, legacy_kind)

    assert [record.kind for record in list_attestations(temp_db, task.id)] == [implementation_kind]


def _legacy_kind() -> str:
    """The review kind the authorship gate exempts, read from its rule text.

    A record of that kind, made before enforcement, must not block closure or landing.
    """
    _implementation, makers = _declared_authorship()
    exempted: set[str] = set()
    for document in _authorship_documents():
        for body in (document.get("rules") or {}).values():
            for group in re.findall(r"not in \[([^\]]*)\]", body.get("when") or ""):
                exempted |= {item.strip().strip("'\"") for item in group.split(",")}
    legacy = exempted - makers
    assert len(legacy) == 1, f"expected one exempted legacy kind, got {sorted(legacy)}"
    (kind,) = legacy
    return kind


def _gate_review_kind(
    decision: ClosureDecision, documents: list[dict[str, Any]], implementation_kind: str
) -> str:
    """The review kind the closure rules behind a blocked decision read, besides implementation."""
    blockers = set(decision.blockers)
    kinds: set[str] = set()
    for document in documents:
        for body in (document.get("rules") or {}).values():
            reasons = {effect.get("reason") for effect in body.get("effects") or []}
            if reasons & blockers:
                kinds |= set(re.findall(r"latest_attestation\('([^']+)'\)", body.get("when") or ""))
    kinds.discard(implementation_kind)
    assert len(kinds) == 1, f"expected one review kind behind the blocker, got {sorted(kinds)}"
    (kind,) = kinds
    return kind


def _research_spike_definition() -> dict[str, Any]:
    """The one bundled definition that writes only implementation, scoped to task types."""
    implementation_kind, _makers = _declared_authorship()
    matches = [
        definition
        for definition in _definitions()
        if set(definition.get("task_attestation_writes") or []) == {implementation_kind}
        and (definition.get("task_attestation_scope") or {}).get("task_types")
    ]
    assert len(matches) == 1, (
        "expected one bundled spike definition that writes only implementation"
    )
    return matches[0]


def _closure_documents_selected_by(definition: dict[str, Any]) -> list[dict[str, Any]]:
    """Closure rule documents that the definition selects by tag."""
    selected = {
        selector.removeprefix("tag:")
        for selector in definition["workflows"]["rule_selectors"]["include"]
        if selector.startswith("tag:")
    }
    return [
        document
        for document in _yaml_files("rules")
        if selected & set(document.get("tags") or [])
        and any(
            body.get("event") in ("task_close", "task_land")
            for body in (document.get("rules") or {}).values()
        )
    ]


def test_research_spike_closes_on_its_implementation_and_an_independent_review(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    spike = _research_spike_definition()
    implementation_kind, _makers = _declared_authorship()
    documents = _closure_documents_selected_by(spike)
    _install_rules(temp_db, project_id, documents)
    seats = AgentSessions(temp_db, project_id)
    author = seats.add(
        spike["name"],
        definition_fields={
            "task_attestation_writes": spike["task_attestation_writes"],
            "task_attestation_kinds": spike.get("task_attestation_kinds") or {},
            "task_attestation_scope": spike["task_attestation_scope"],
            "workflows": {"rule_selectors": spike["workflows"]["rule_selectors"]},
        },
    )
    reviewer = seats.add("synthetic-reviewer", definition_fields={"task_attestation_writes": ["*"]})
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id,
        "Survey the options",
        task_type=spike["task_attestation_scope"]["task_types"][0],
        category=spike["task_close_scope"]["categories"][0],
        validation_criteria="Criteria hold.",
    )
    tasks.claim_task(task.id, author)
    candidate_sha = "b" * 40
    candidate = {"scheme": "git", "value": candidate_sha}
    record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(
            kind=implementation_kind, result="approved", summary="Observed.", reference=candidate
        ),
    )
    rules = RuleDefinitionManager(temp_db).list_effective(project_id)
    closer = AgentDefinitionBody.model_validate(
        {
            "name": spike["name"],
            "prompts": {"agent": "Close the task."},
            "workflows": {
                "rule_selectors": {"include": spike["workflows"]["rule_selectors"]["include"]}
            },
        }
    )

    def closing_snapshot() -> ClosureSnapshot:
        return ClosureSnapshot(
            event="task_close",
            task={"id": task.id},
            attestations=tuple(list_attestations(temp_db, task.id)),
            # Provenance the daemon supplies for a candidate under a trusted project policy.
            # The candidate touches no security path, so no security approval is required.
            facts={
                "candidate_commit_sha": candidate_sha,
                "project_policy_available": True,
                "candidate_paths_available": True,
                "candidate_paths": [".gobby/plans/research/report.md"],
                "project_policy": {"security_paths": []},
            },
        )

    before = evaluate_closure_policy(closing_snapshot(), agent=closer, rules=rules)
    assert not before.allowed
    assert any("candidate review" in reason for reason in before.blockers)
    review_kind = _gate_review_kind(before, documents, implementation_kind)

    record_attestation(
        temp_db,
        task_id=task.id,
        session_id=reviewer,
        submission=AttestationSubmission(
            kind=review_kind, result="approved", summary="Observed.", reference=candidate
        ),
    )
    after = evaluate_closure_policy(closing_snapshot(), agent=closer, rules=rules)

    assert after.allowed, after.blockers
