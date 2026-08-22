from pathlib import Path
from unittest.mock import MagicMock

from jsonschema.validators import validator_for

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry
from gobby.plans.review_findings import FINDING_ITEM_SCHEMA, FINDING_SEVERITIES

TASKLESS_AGENT_PATH = Path("src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml")
STAGED_AGENT_PATH = Path("src/gobby/install/shared/workflows/agents/plan-adversary.yaml")


def test_finding_schema_parity_with_adversary_contracts() -> None:
    registry = create_stage_ops_registry(RegistryContext(task_manager=MagicMock()))
    schema = registry._tools["reject_review"].input_schema
    finding_schema = schema["properties"]["findings"]["items"]

    required = set(finding_schema["required"])
    assert required == {
        "finding_id",
        "section_id",
        "check_key",
        "severity",
        "category",
        "location",
        "description",
        "fix",
        "prevention",
    }
    assert set(finding_schema["properties"]["severity"]["enum"]) == FINDING_SEVERITIES

    validator = validator_for(finding_schema)(finding_schema)
    validator.validate(
        {
            "finding_id": "F1",
            "section_id": "1.1",
            "check_key": "failure-atomicity",
            "severity": "blocking",
            "category": "unhandled-edge",
            "location": "§ 1.1",
            "description": "The failure path leaves partial state.",
            "fix": "Specify rollback before retry.",
            "prevention": "Walk every write failure boundary.",
            "root_cause": "Only the success path was modeled.",
        }
    )

    contracts = TASKLESS_AGENT_PATH.read_text(encoding="utf-8") + STAGED_AGENT_PATH.read_text(
        encoding="utf-8"
    )
    assert "severity: <blocking | nit>" in contracts
    assert "fix: <concrete plan change>" in contracts


def test_reject_review_uses_shared_finding_schema() -> None:
    registry = create_stage_ops_registry(RegistryContext(task_manager=MagicMock()))
    finding_schema = registry._tools["reject_review"].input_schema["properties"]["findings"][
        "items"
    ]

    assert finding_schema is FINDING_ITEM_SCHEMA
    assert "repairs" in finding_schema["properties"]
    validator = validator_for(finding_schema)(finding_schema)
    validator.validate(
        {
            "finding_id": "F1",
            "section_id": "1.1",
            "check_key": "targets-complete",
            "severity": "blocking",
            "category": "traceability",
            "location": "§ 1.1 Targets",
            "description": "The consumer file is missing from Targets.",
            "fix": "Add the consumer to Targets.",
            "prevention": "Run the consumer sweep.",
            "root_cause": "Only direct edits were inventoried.",
            "repairs": [
                {"kind": "add_targets", "section_id": "1.1", "entries": ["`src/consumer.py::use`"]}
            ],
        }
    )
