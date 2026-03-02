"""Tests for workflow templates module."""

from __future__ import annotations

import json

import pytest

from gobby.workflows.workflow_templates import get_template_by_id, get_workflow_templates

pytestmark = pytest.mark.unit


class TestGetWorkflowTemplates:
    """Tests for get_workflow_templates()."""

    def test_returns_list_of_templates(self) -> None:
        templates = get_workflow_templates()
        assert isinstance(templates, list)
        assert len(templates) == 4

    def test_each_template_has_required_fields(self) -> None:
        required = {"id", "name", "description", "workflow_type", "definition_json"}
        for t in get_workflow_templates():
            assert required.issubset(t.keys()), f"Template {t.get('id')} missing fields"

    def test_each_definition_json_is_valid(self) -> None:
        for t in get_workflow_templates():
            parsed = json.loads(t["definition_json"])
            assert isinstance(parsed, dict)
            assert "steps" in parsed

    def test_workflow_types_are_valid(self) -> None:
        valid_types = {"workflow", "pipeline"}
        for t in get_workflow_templates():
            assert t["workflow_type"] in valid_types

    def test_ids_are_unique(self) -> None:
        ids = [t["id"] for t in get_workflow_templates()]
        assert len(ids) == len(set(ids))

    def test_blank_workflow_template(self) -> None:
        t = get_template_by_id("blank-workflow")
        assert t is not None
        assert t["workflow_type"] == "workflow"
        defn = json.loads(t["definition_json"])
        assert len(defn["steps"]) == 1
        assert defn["steps"][0]["name"] == "work"

    def test_tdd_developer_has_three_steps(self) -> None:
        t = get_template_by_id("tdd-developer")
        assert t is not None
        defn = json.loads(t["definition_json"])
        assert len(defn["steps"]) == 3
        step_names = [s["name"] for s in defn["steps"]]
        assert step_names == ["red", "green", "blue"]

    def test_blank_pipeline_template(self) -> None:
        t = get_template_by_id("blank-pipeline")
        assert t is not None
        assert t["workflow_type"] == "pipeline"
        defn = json.loads(t["definition_json"])
        assert defn["type"] == "pipeline"

    def test_ci_pipeline_has_approval_gate(self) -> None:
        t = get_template_by_id("ci-pipeline")
        assert t is not None
        defn = json.loads(t["definition_json"])
        approval_steps = [s for s in defn["steps"] if "approval" in s]
        assert len(approval_steps) == 1

    def test_get_template_by_id_returns_none_for_unknown(self) -> None:
        assert get_template_by_id("nonexistent") is None
