from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import yaml
from jsonschema.validators import validator_for

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry
from gobby.plans.review_findings import FINDING_SEVERITIES

AGENT_PATH = (
    Path(__file__).parents[2]
    / "src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml"
)


def test_severity_enum_parity_with_findings() -> None:
    registry = create_stage_ops_registry(RegistryContext(task_manager=MagicMock()))
    schema = registry._tools["reject_review"].input_schema
    finding_schema = schema["properties"]["findings"]["items"]

    assert set(finding_schema["properties"]["severity"]["enum"]) == FINDING_SEVERITIES
    assert "minimal_repair" in finding_schema["required"]
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
    validator(schema).validate(
        {
            "task_id": "#1",
            "stage_name": "planning",
            "findings": [finding],
        }
    )

    agent = yaml.safe_load(AGENT_PATH.read_text(encoding="utf-8"))
    instructions = cast(str, agent["instructions"])
    finding_contract = instructions.split("If blocking findings remain", 1)[1].split(
        "If requirements are insufficient",
        1,
    )[0]
    assert "minimal_repair:" in finding_contract
    assert "failure_trace:" in finding_contract
    assert "suggested_fix:" not in finding_contract
