from unittest.mock import MagicMock

from jsonschema.validators import validator_for

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry
from gobby.plans.review_findings import FINDING_ITEM_SCHEMA


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
