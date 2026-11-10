"""``capabilities: ["*"]`` passes every restriction check and switches on no behavior."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gobby.agents.team_communications import admits_sender
from gobby.tasks.closure_policy import build_closure_condition_helpers
from gobby.workflows.agent_models import (
    ALL_CAPABILITIES,
    AgentDefinitionBody,
    capabilities_contain,
    holds_capability,
)

pytestmark = pytest.mark.unit

# A behavior switch no bundled definition names: read literally, never granted by "*".
_SYNTHETIC_BEHAVIOR = "synthetic_behavior_switch"


def _body(**fields: object) -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(
        {
            "name": "synthetic-seat",
            "prompts": {"agent": "Act."},
            "workflows": {"rule_selectors": {"include": []}},
            **fields,
        }
    )


def test_wildcard_holds_any_permission_capability() -> None:
    assert holds_capability([ALL_CAPABILITIES], "spawn_any_definition")
    assert holds_capability([ALL_CAPABILITIES], "a_capability_no_definition_names_yet")
    assert holds_capability(["land_tasks"], "land_tasks")
    assert not holds_capability(["land_tasks"], "stop_any_seat")
    assert not holds_capability([], "land_tasks")


def test_wildcard_never_switches_on_a_behavior() -> None:
    wildcard = _body(capabilities=[ALL_CAPABILITIES])
    named = _body(capabilities=[_SYNTHETIC_BEHAVIOR])
    assert not wildcard.declares(_SYNTHETIC_BEHAVIOR)
    assert named.declares(_SYNTHETIC_BEHAVIOR)
    assert not _body(capabilities=["land_tasks"]).declares(_SYNTHETIC_BEHAVIOR)


def test_body_holds_uses_the_wildcard() -> None:
    body = _body(capabilities=[ALL_CAPABILITIES])
    assert body.holds("approve_security_review")
    assert body.holds(_SYNTHETIC_BEHAVIOR)


def test_wildcard_stands_alone() -> None:
    with pytest.raises(ValidationError, match="never both"):
        _body(capabilities=[ALL_CAPABILITIES, "land_tasks"])


def test_wildcard_caller_is_admitted_by_any_spawner_requirement() -> None:
    target = _body(spawner_capabilities=["spawn_any_definition"])
    assert target.admits_spawner([ALL_CAPABILITIES])
    assert not target.admits_spawner(["land_tasks"])
    assert _body().admits_spawner([])


def test_wildcard_caller_contains_every_target() -> None:
    assert capabilities_contain([ALL_CAPABILITIES], ["stop_any_seat", "send_keys"])
    assert capabilities_contain([ALL_CAPABILITIES], [ALL_CAPABILITIES])
    assert capabilities_contain(["a", "b"], ["a"])
    assert not capabilities_contain(["a"], ["a", "b"])
    assert not capabilities_contain(["a"], [ALL_CAPABILITIES])


def test_wildcard_containment_covers_targets_that_name_behaviors() -> None:
    assert capabilities_contain([ALL_CAPABILITIES], ["land_tasks", _SYNTHETIC_BEHAVIOR])


def test_wildcard_direct_integration_grant_needs_its_globs() -> None:
    assert _body(
        capabilities=[ALL_CAPABILITIES], direct_integration_commit_paths=["**"]
    ).covers_direct_integration_paths(["src/a.py"])
    assert not _body(capabilities=[ALL_CAPABILITIES]).covers_direct_integration_paths(["src/a.py"])


def test_wildcard_sender_is_admitted_by_restricted_recipients() -> None:
    recipient = _body(message_sender_capabilities=["message_restricted_seats"])
    assert admits_sender(recipient, [ALL_CAPABILITIES])
    assert not admits_sender(recipient, ["land_tasks"])


def test_closure_rules_read_attestation_capabilities_through_the_wildcard() -> None:
    holds = build_closure_condition_helpers({"attestations": []})["holds_capability"]
    assert holds([ALL_CAPABILITIES], "approve_security_review")
    assert not holds(["land_tasks"], "approve_security_review")
