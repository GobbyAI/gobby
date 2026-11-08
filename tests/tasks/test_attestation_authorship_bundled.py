"""Bundled definitions honor the implementer-review rule, enumerated generically.

The rule's authorship split is read from the bundled YAML. Nothing here names a bundled
definition, rule or pipeline except the exempt `default` definition.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.definitions import RuleDefinitionBody

pytestmark = pytest.mark.unit

_WORKFLOWS = Path(__file__).parents[2] / "src/gobby/install/shared/workflows"
_EXEMPT = "default"
_TAG = "task-closure-implementer-review"


def _yaml_files(subdir: str) -> list[dict[str, Any]]:
    return [
        yaml.safe_load(path.read_text()) for path in sorted((_WORKFLOWS / subdir).rglob("*.yaml"))
    ]


def _definitions() -> list[dict[str, Any]]:
    return _yaml_files("agents")


def _authorship_rules() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    found = []
    for document in _yaml_files("rules"):
        for body in (document.get("rules") or {}).values():
            if isinstance(body, dict) and "attestation_authorship" in body:
                found.append((document, body))
    return found


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
    return f"tag:{_TAG}" in include


def test_bundled_authorship_declarations_are_valid_closure_rules() -> None:
    assert _authorship_rules(), "no bundled rule declares attestation_authorship"
    for _document, body in _authorship_rules():
        validated = RuleDefinitionBody.model_validate(body)
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
