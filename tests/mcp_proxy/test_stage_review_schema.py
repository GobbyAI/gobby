from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
import yaml
from jsonschema import ValidationError
from jsonschema.validators import validator_for

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry
from gobby.plans.review_findings import FINDING_SEVERITIES

TASKLESS_AGENT_PATH = (
    Path(__file__).parents[2]
    / "src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml"
)
STAGED_AGENT_PATH = (
    Path(__file__).parents[2] / "src/gobby/install/shared/workflows/agents/plan-adversary.yaml"
)


def test_finding_schema_parity_with_adversary_contracts() -> None:
    registry = create_stage_ops_registry(RegistryContext(task_manager=MagicMock()))
    schema = registry._tools["reject_review"].input_schema
    finding_schema = schema["properties"]["findings"]["items"]

    assert set(finding_schema["properties"]["severity"]["enum"]) == FINDING_SEVERITIES
    assert "minimal_repair" in finding_schema["required"]
    assert "repair_scope" in finding_schema["required"]
    assert set(finding_schema["properties"]["repair_scope"]["enum"]) == {
        "existing_sections",
        "new_deliverable",
    }
    assert "fix" not in finding_schema["properties"]
    assert "suggested_fix" not in finding_schema["properties"]

    finding = {
        "finding_id": "F1",
        "section_id": "1.1",
        "check_key": "failure-atomicity",
        "severity": "blocking",
        "category": "unhandled-edge",
        "location": "§ 1.1",
        "description": "The failure path can leave partial state.",
        "minimal_repair": "Specify rollback before retry.",
        "repair_scope": "existing_sections",
        "principle": "Failure handling must be atomic.",
        "prevention": "Walk every write failure boundary.",
        "failure_trace": {
            "preconditions": "The first durable write succeeds.",
            "action": "The second durable write fails.",
            "wrong_outcome": "The first write remains visible.",
            "violated_obligation": "The operation must commit atomically.",
            "citation": [
                {
                    "path": "src/gobby/plans/review_findings.py",
                    "sha256": "c" * 64,
                    "line_start": 117,
                    "line_end": 158,
                }
            ],
        },
    }
    validator = validator_for(schema)
    validator.check_schema(schema)
    validate = validator(schema).validate
    payload = {
        "task_id": "#1",
        "stage_name": "planning",
        "findings": [finding],
    }
    validate(payload)

    finding["new_deliverable_justification"] = "A separate artifact is easier to find."
    with pytest.raises(ValidationError):
        validate(payload)

    finding["repair_scope"] = "new_deliverable"
    validate(payload)
    finding.pop("new_deliverable_justification")
    with pytest.raises(ValidationError):
        validate(payload)

    taskless_agent = yaml.safe_load(TASKLESS_AGENT_PATH.read_text(encoding="utf-8"))
    taskless_instructions = cast(str, taskless_agent["instructions"])
    finding_contract = taskless_instructions.split("If blocking findings remain", 1)[1].split(
        "If requirements are insufficient",
        1,
    )[0]
    assert "minimal_repair:" in finding_contract
    assert "repair_scope:" in finding_contract
    assert "new_deliverable_justification:" in finding_contract
    assert "failure_trace:" in finding_contract
    assert "suggested_fix:" not in finding_contract

    staged_agent = yaml.safe_load(STAGED_AGENT_PATH.read_text(encoding="utf-8"))
    staged_instructions = cast(str, staged_agent["instructions"])
    evidence_contract = staged_instructions.split("PLAN REVIEW EVIDENCE CONTRACT:", 1)[1].split(
        "THREE-LANE COVERAGE PROTOCOL:",
        1,
    )[0]
    assert "repair_scope" in evidence_contract
    assert "new_deliverable_justification" in evidence_contract
