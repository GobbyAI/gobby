"""Tests for agent_resolver.resolve_agent() and typed-manager consumers."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from gobby.agents.sync import sync_bundled_agents
from gobby.dispatch.skill_composition import inspect_skill_composition
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.workflows.agent_resolver import resolve_agent, resolve_agent_with_row
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.dry_run import evaluate_agent_definition
from gobby.workflows.engine.core import RuleEngine

pytest_plugins = ["tests.storage.definitions.conftest"]

pytestmark = pytest.mark.unit

_STEP_WORKFLOW: dict[str, Any] = {
    "variables": {"required_skills": ["tdd"], "goal": "ship"},
    "exit_condition": "done",
    "steps": [
        {"name": "implement", "prompt": "write the code"},
        {"name": "review", "prompt": "check the diff"},
    ],
}


def _manager(db: PostgresHubDatabase) -> AgentDefinitionManager:
    return AgentDefinitionManager(db)


def _create_agent(
    db: PostgresHubDatabase,
    name: str,
    *,
    body: dict[str, Any] | None = None,
    step_workflow: dict[str, Any] | None = None,
    project_id: str | None = None,
    source: str = "custom",
) -> Any:
    payload = {"name": name, "provider": "claude", **(body or {})}
    manager = _manager(db)
    if step_workflow is not None:
        return manager.upsert_with_steps(
            name,
            payload,
            step_workflow,
            project_id=project_id,
            source=source,  # type: ignore[arg-type]
        )
    return manager.create(name, payload, project_id=project_id, source=source)  # type: ignore[arg-type]


class TestResolveAgentDefault:
    """resolve_agent('default', db) returns Pydantic defaults when no DB record exists."""

    def test_default_returns_pydantic_defaults_when_no_db_record(
        self, definition_db: PostgresHubDatabase
    ) -> None:
        result = resolve_agent("default", definition_db)
        assert result is not None
        assert isinstance(result, AgentDefinitionBody)
        assert result.name == "default"
        assert result.provider == "claude"

    def test_default_resolves_inherit_from_cli_source(
        self, definition_db: PostgresHubDatabase
    ) -> None:
        result = resolve_agent("default", definition_db, cli_source="codex")
        assert result is not None
        assert result.name == "default"
        assert result.provider == "codex"

    def test_default_uses_db_record_when_present(self, definition_db: PostgresHubDatabase) -> None:
        _create_agent(
            definition_db,
            "default",
            body={"role": "custom default role", "provider": "claude"},
        )

        result = resolve_agent("default", definition_db)
        assert result is not None
        assert result.name == "default"
        assert result.role == "custom default role"
        assert result.provider == "claude"

    def test_nonexistent_agent_returns_none(self, definition_db: PostgresHubDatabase) -> None:
        result = resolve_agent("nonexistent", definition_db)
        assert result is None


class TestResolveAgentLookup:
    """resolve_agent does a typed-manager lookup with hydrated step_workflow."""

    def test_simple_lookup(self, definition_db: PostgresHubDatabase) -> None:
        _create_agent(
            definition_db,
            "developer",
            body={"role": "Backend developer", "provider": "claude"},
        )
        result = resolve_agent("developer", definition_db)
        assert result is not None
        assert result.name == "developer"
        assert result.role == "Backend developer"
        assert result.provider == "claude"
        assert result.step_workflow is None

    def test_hydrates_nested_step_workflow(self, definition_db: PostgresHubDatabase) -> None:
        created = _create_agent(definition_db, "coder", step_workflow=_STEP_WORKFLOW)
        result = resolve_agent("coder", definition_db)
        assert result is not None
        assert result.step_workflow is not None
        assert [step.name for step in result.step_workflow.steps] == ["implement", "review"]
        assert result.step_workflow.variables["required_skills"] == ["tdd"]

        found = resolve_agent_with_row("coder", definition_db)
        assert found is not None
        body, row = found
        assert body.step_workflow is not None
        assert row.step_workflow_id == created.step_workflow_id
        assert row.step_workflow_id is not None

    def test_skips_non_agent_type(self, definition_db: PostgresHubDatabase) -> None:
        RuleDefinitionManager(definition_db).create(
            name="my-rule",
            definition_json={"event": "before_tool", "effect": {"type": "block", "reason": "no"}},
            source="custom",
        )
        result = resolve_agent("my-rule", definition_db)
        assert result is None

    def test_project_rule_does_not_shadow_global_agent(
        self, definition_db: PostgresHubDatabase
    ) -> None:
        project_id = str(uuid4())
        _create_agent(definition_db, "shared-name", body={"provider": "codex"})
        RuleDefinitionManager(definition_db).create(
            name="shared-name",
            definition_json={"event": "before_tool", "effects": []},
            project_id=project_id,
            source="custom",
        )

        result = resolve_agent("shared-name", definition_db, project_id=project_id)

        assert result is not None
        assert result.name == "shared-name"
        assert result.provider == "codex"

    def test_project_agent_shadows_global_agent(self, definition_db: PostgresHubDatabase) -> None:
        project_id = str(uuid4())
        _create_agent(definition_db, "shared-name", body={"role": "global"})
        _create_agent(
            definition_db,
            "shared-name",
            body={"role": "project"},
            project_id=project_id,
        )

        project_hit = resolve_agent("shared-name", definition_db, project_id=project_id)
        global_hit = resolve_agent("shared-name", definition_db)

        assert project_hit is not None
        assert project_hit.role == "project"
        assert global_hit is not None
        assert global_hit.role == "global"

    def test_invalid_agent_definition_logs_warning_with_traceback(
        self,
        definition_db: PostgresHubDatabase,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _manager(definition_db).create(
            name="broken-agent",
            definition_json={"name": "broken-agent", "surfaces": [123]},
            source="custom",
        )

        with caplog.at_level(logging.WARNING, logger="gobby.workflows.agent_resolver"):
            result = resolve_agent("broken-agent", definition_db)

        assert result is None
        assert "Failed to parse agent definition for broken-agent" in caplog.text
        assert any(record.exc_info for record in caplog.records)


class TestProviderInheritance:
    """Provider 'inherit' is resolved based on cli_source."""

    def test_inherit_resolved_to_claude_by_default(
        self, definition_db: PostgresHubDatabase
    ) -> None:
        _create_agent(definition_db, "test", body={"provider": "inherit"})
        result = resolve_agent("test", definition_db)
        assert result is not None
        assert result.provider == "claude"

    @pytest.mark.parametrize("cli_source", ["claude", "codex", "droid"])
    def test_inherit_resolved_from_supported_cli_source(
        self,
        definition_db: PostgresHubDatabase,
        cli_source: str,
    ) -> None:
        _create_agent(definition_db, "test2", body={"provider": "inherit"})
        result = resolve_agent("test2", definition_db, cli_source=cli_source)
        assert result is not None
        assert result.provider == cli_source

    def test_inherit_preserves_unknown_cli_source(self, definition_db: PostgresHubDatabase) -> None:
        _create_agent(definition_db, "test3", body={"provider": "inherit"})
        result = resolve_agent("test3", definition_db, cli_source="custom-provider")
        assert result is not None
        assert result.provider == "custom-provider"

    def test_explicit_provider_not_overridden(self, definition_db: PostgresHubDatabase) -> None:
        _create_agent(definition_db, "test4", body={"provider": "codex"})
        result = resolve_agent("test4", definition_db, cli_source="claude")
        assert result is not None
        assert result.provider == "codex"


class TestStepfulAndSteplessConsumers:
    """Resolution, dry-run, and required-skills work for both agent shapes."""

    @pytest.mark.asyncio
    async def test_stepful_and_stepless_resolution_dry_run_and_skills(
        self, definition_db: PostgresHubDatabase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _create_agent(definition_db, "stepless", body={"role": "plain"})
        _create_agent(
            definition_db,
            "stepful",
            body={"role": "guided"},
            step_workflow=_STEP_WORKFLOW,
        )

        stepless = resolve_agent("stepless", definition_db)
        stepful = resolve_agent("stepful", definition_db)
        assert stepless is not None
        assert stepless.step_workflow is None
        assert stepful is not None
        assert stepful.step_workflow is not None

        stepless_eval = await evaluate_agent_definition(stepless)
        stepful_eval = await evaluate_agent_definition(stepful)
        assert stepless_eval.valid is True
        assert any(item.code == "NO_STEP_WORKFLOW" for item in stepless_eval.items)
        assert stepful_eval.valid is True
        assert [trace.name for trace in stepful_eval.step_trace] == ["implement", "review"]
        assert not any(item.code == "NO_STEP_WORKFLOW" for item in stepful_eval.items)

        project_id = str(uuid4())
        monkeypatch.setattr(
            "gobby.dispatch.skill_composition.LocalSkillManager.list_skills",
            lambda self, **_kwargs: [],
        )
        stepless_skills = inspect_skill_composition(
            definition_db,
            project_id=project_id,
            agent_body=stepless,
            additional_skills=(),
        )
        stepful_skills = inspect_skill_composition(
            definition_db,
            project_id=project_id,
            agent_body=stepful,
            additional_skills=(),
        )
        assert stepless_skills.required_skills == ()
        assert stepful_skills.required_skills == ("tdd",)


class TestCachedHydratedAgent:
    def test_child_only_step_workflow_edit_invalidates_cache(
        self, definition_db: PostgresHubDatabase
    ) -> None:
        created = _create_agent(definition_db, "coder", step_workflow=_STEP_WORKFLOW)
        engine = RuleEngine(definition_db, skill_script_materializer=MagicMock())

        first = engine._load_active_agent_definition("coder")
        assert first is not None
        assert first.step_workflow is not None
        assert first.step_workflow.variables["required_skills"] == ["tdd"]
        assert [step.name for step in first.step_workflow.steps] == ["implement", "review"]

        _manager(definition_db).set_step_workflow(
            created.id,
            {
                "variables": {"required_skills": ["review"]},
                "steps": [{"name": "review", "prompt": "look"}],
            },
        )
        second = engine._load_active_agent_definition("coder")
        assert second is not None
        assert second.step_workflow is not None
        assert [step.name for step in second.step_workflow.steps] == ["review"]
        assert second.step_workflow.variables["required_skills"] == ["review"]

        _manager(definition_db).set_step_workflow(created.id, None)
        third = engine._load_active_agent_definition("coder")
        assert third is not None
        assert third.step_workflow is None


def test_memory_recall_helper_is_not_resolvable_agent(definition_db: PostgresHubDatabase) -> None:
    """Memory recall no longer has a bundled spawnable helper agent."""
    sync_result = sync_bundled_agents(definition_db)

    assert sync_result["success"] is True
    assert sync_result["errors"] == []

    result = resolve_agent("memory-recall-helper", definition_db)

    assert result is None
