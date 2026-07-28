from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from jsonschema import ValidationError
from jsonschema.validators import validator_for

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.plans.review_evidence import register_review_evidence_tools
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.storage.hub.protocol import HubDatabase


def _registry(db: HubDatabase) -> InternalToolRegistry:
    registry = InternalToolRegistry("gobby-plans")
    register_review_evidence_tools(
        registry,
        db,
        resolve_project_id=lambda _project: "project-id",
    )
    return registry


def _tool_schema(registry: InternalToolRegistry, name: str) -> dict[str, Any]:
    metadata = registry.get_schema(name)
    assert metadata is not None
    return cast(dict[str, Any], metadata["inputSchema"])


def _assert_no_bare_object_schema(schema: object, *, path: str) -> None:
    if isinstance(schema, dict):
        declared_type = schema.get("type")
        object_typed = declared_type == "object" or (
            isinstance(declared_type, list) and "object" in declared_type
        )
        if object_typed:
            properties = schema.get("properties")
            assert isinstance(properties, dict), f"{path} lacks properties"
            assert "required" in schema, f"{path} lacks required"
            assert schema.get("additionalProperties") is False or isinstance(
                schema.get("additionalProperties"), dict
            ), f"{path} has an unconstrained additionalProperties surface"
        for key, value in schema.items():
            _assert_no_bare_object_schema(value, path=f"{path}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            _assert_no_bare_object_schema(value, path=f"{path}[{index}]")


def test_structured_payloads_publish_nested_schemas_and_examples(
    temp_db: HubDatabase,
) -> None:
    registry = _registry(temp_db)
    structured_tools = {
        "verify_plan_review_index_token",
        "prepare_plan_review_round",
        "derive_plan_review_manifest",
        "validate_plan_review_coverage",
        "apply_plan_review_manifest",
        "render_v1_round_checkpoint",
        "finalize_plan_review_evidence",
        "checkpoint_plan_review_lesson_mint",
    }

    for tool_name in structured_tools:
        metadata = registry.get_schema(tool_name)
        assert metadata is not None
        assert "Example:" in metadata["description"]
        schema = cast(dict[str, Any], metadata["inputSchema"])
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        _assert_no_bare_object_schema(schema, path=tool_name)


def test_disposition_schema_rejects_malformed(temp_db: HubDatabase) -> None:
    registry = _registry(temp_db)
    tool_metadata = registry.get_schema("validate_plan_review_coverage")
    assert tool_metadata is not None
    assert "Example:" in tool_metadata["description"]
    tool_schema = cast(dict[str, Any], tool_metadata["inputSchema"])
    properties = cast(dict[str, Any], tool_schema["properties"])
    assert "shadow_manifest_status" not in properties

    routing_schema = cast(dict[str, Any], properties["routing_decisions"])
    decision_schema = cast(dict[str, Any], routing_schema["additionalProperties"])
    assert decision_schema["properties"]
    assert "required" in decision_schema
    assert decision_schema["additionalProperties"] is False

    bundle_schema = cast(dict[str, Any], properties["candidate_dispositions"])
    bundle_properties = cast(dict[str, Any], bundle_schema["properties"])
    dispositions_schema = cast(dict[str, Any], bundle_properties["candidate_dispositions"])
    disposition_schema = cast(dict[str, Any], dispositions_schema["items"])
    assert set(disposition_schema["required"]) == {
        "candidate_id",
        "check_key",
        "source_section_ids",
        "source_hash",
        "disposition",
        "rationale",
    }
    assert disposition_schema["additionalProperties"] is False

    validator_class = validator_for(disposition_schema)
    validator_class.check_schema(disposition_schema)
    validate = validator_class(disposition_schema).validate
    record: dict[str, object] = {
        "candidate_id": "candidate-1",
        "check_key": "failure-atomicity",
        "source_section_ids": ["1.1"],
        "source_hash": "a" * 64,
        "disposition": "dismissed",
        "rationale": "The candidate is covered by the existing rollback requirement.",
    }
    validate(record)

    record["rationale"] = 42
    with pytest.raises(ValidationError) as exc_info:
        validate(record)
    assert list(exc_info.value.absolute_path) == ["rationale"]
    assert exc_info.value.validator == "type"


@pytest.mark.asyncio
async def test_preparation_payload_schema_round_trip(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def prepare(
        _service: PlanReviewEvidenceService,
        **kwargs: object,
    ) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(to_dict=lambda: {"evidence_id": "evidence-2"})

    monkeypatch.setattr(PlanReviewEvidenceService, "prepare_plan_review_round", prepare)
    registry = _registry(temp_db)
    tool_metadata = registry.get_schema("prepare_plan_review_round")
    assert tool_metadata is not None
    assert "Example:" in tool_metadata["description"]
    schema = _tool_schema(registry, "prepare_plan_review_round")
    properties = cast(dict[str, Any], schema["properties"])

    resolution_schema = cast(dict[str, Any], properties["prior_finding_resolutions"]["items"])
    assert set(resolution_schema["required"]) == {"prior_finding_id", "decision"}
    assert resolution_schema["additionalProperties"] is False
    attestation_schema = cast(dict[str, Any], properties["repair_attestations"]["items"])
    assert {
        "prior_finding_id",
        "check_key",
        "changed_section_ids",
        "accepted_resolution",
        "deviation_from_minimal_repair",
        "changed_symbols",
        "consumer_sites_swept",
        "adjacent_variants_swept",
        "validation_evidence",
        "deferred_sites",
    } <= set(attestation_schema["required"])
    assert attestation_schema["additionalProperties"] is False

    prior_finding_resolutions = [
        {
            "prior_finding_id": "finding-1",
            "decision": "repair",
        }
    ]
    repair_attestations = [
        {
            "prior_finding_id": "finding-1",
            "check_key": "failure-atomicity",
            "changed_section_ids": ["1.1"],
            "accepted_resolution": "Add an explicit rollback requirement.",
            "deviation_from_minimal_repair": None,
            "changed_symbols": ["PlanReviewEvidenceService.prepare_plan_review_round"],
            "consumer_sites_swept": ["src/gobby/plans/review_evidence.py"],
            "adjacent_variants_swept": ["interactive review"],
            "validation_evidence": ["focused schema tests pass"],
            "deferred_sites": [],
        }
    ]
    payload = {
        "plan_path": "/repo/.gobby/plans/example.md",
        "round_number": 2,
        "prior_finding_resolutions": prior_finding_resolutions,
        "repair_attestations": repair_attestations,
    }
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    validator_class(schema).validate(payload)

    result = await registry.call("prepare_plan_review_round", payload)

    assert result["ok"] is True
    assert captured["prior_finding_resolutions"] == prior_finding_resolutions
    assert captured["repair_attestations"] == repair_attestations
