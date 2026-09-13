"""Isolated storage coverage for retirement-time instruction migration."""

from typing import Any
from unittest.mock import patch

import pytest

from gobby.skills.capability_catalog import load_capability_catalog
from gobby.skills.reference_migration import migrate_instruction_requirements
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.fixtures.agent_definitions import make_agent_definition


def test_reference_contract_5_1_1(temp_db: HubDatabase) -> None:
    """Owned instruction fields convert once; other content and enabled pins survive."""
    catalog = load_capability_catalog()
    agents = AgentDefinitionManager(temp_db)
    agent = agents.upsert_with_steps(
        "migration-agent",
        {"name": "migration-agent", "prompts": {"agent": "Load tasks"}},
        {
            "variables": {
                "required_skills": ["tasks", "restraint", catalog.folded_skills["tasks"]],
                "additional_skills": ["plan-draft"],
                "loaded_skills": ["tasks"],
            },
            "steps": [],
        },
        tags=["gobby"],
    )
    agents.toggle_enabled(agent.id)
    defaults = SessionVariableDefaultManager(temp_db)
    default = defaults.create("required_skills", ["memory"], tags=["gobby"])
    rules = RuleDefinitionManager(temp_db)
    rule = rules.create(
        "migration-rule",
        {
            "effects": [
                {"type": "set_variable", "variable": "additional_skills", "value": ["merge"]},
                {"type": "load_skill", "skill": "plan"},
            ],
            "description": "merge tasks",
        },
        tags=["gobby"],
    )

    result = migrate_instruction_requirements(temp_db, catalog)
    assert result.errors == []
    assert result.updated == 3
    updated = agents.get(agent.id)
    assert updated.enabled is False
    assert updated.enabled_pinned is True
    assert updated.definition_json["prompts"] == {"agent": "Load tasks"}
    variables = updated.definition_json["step_workflow"]["variables"]
    assert variables["required_skills"] == [catalog.folded_skills["tasks"], "restraint"]
    assert variables["additional_skills"] == [catalog.folded_skills["plan-draft"]]
    assert variables["loaded_skills"] == ["tasks"]
    assert defaults.get(default.id).default_value == [catalog.folded_skills["memory"]]
    migrated_rule = rules.get(rule.id).definition_json
    assert migrated_rule["effects"][0]["value"] == [catalog.folded_skills["merge"]]
    assert migrated_rule["effects"][1]["skill"] == catalog.folded_skills["plan"]
    assert migrated_rule["description"] == "merge tasks"
    repeated = migrate_instruction_requirements(temp_db, catalog)
    assert repeated.updated == 0
    assert repeated.errors == []
    assert agents.get(agent.id).updated_at == updated.updated_at


def test_reference_contract_5_1_2(temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
    """Custom rows retain their requirements with exact, actionable replacements."""
    catalog = load_capability_catalog()
    agents = AgentDefinitionManager(temp_db)
    agent = agents.upsert_with_steps(
        "custom-agent",
        {"name": "custom-agent", "prompts": {"agent": "tasks"}},
        {"variables": {"required_skills": ["tasks", "brevity"]}, "steps": []},
        source="custom",
        tags=["gobby"],
    )
    defaults = SessionVariableDefaultManager(temp_db)
    default = defaults.create("additional_skills", ["memory"], source="custom")
    project_agent = agents.upsert_with_steps(
        "project-agent",
        {"name": "project-agent", "prompts": {"agent": "Project guidance"}},
        {"variables": {"required_skills": ["tasks"]}, "steps": []},
        project_id=sample_project["id"],
        source="installed",
        tags=["gobby"],
    )
    result = migrate_instruction_requirements(temp_db, catalog)
    assert result.errors == []
    assert result.updated == 0
    assert agents.get(agent.id) == agent
    assert defaults.get(default.id) == default
    assert agents.get(project_agent.id) == project_agent
    assert any(
        agent.id in warning
        and "step_workflow.variables.required_skills" in warning
        and "tasks -> gobby:references/tasks/overview.md" in warning
        for warning in result.warnings
    )
    assert any(default.id in warning and "memory ->" in warning for warning in result.warnings)

    from gobby.skills.sync import sync_bundled_skills
    from gobby.storage.skills import LocalSkillManager

    skills = LocalSkillManager(temp_db)
    custom = skills.create_skill(
        name="gusto",
        description="User integration",
        content="Private custom methodology",
        project_id=sample_project["id"],
        source_type="local",
    )
    override = skills.create_skill(
        name="tasks",
        description="Project task method",
        content="Custom override",
        project_id=sample_project["id"],
        metadata={"gobby": {"audience": "all"}},
    )
    synced = sync_bundled_skills(temp_db)
    assert synced["success"] is True, synced["errors"]
    assert skills.get_by_name("gusto", project_id=sample_project["id"]) == custom
    assert skills.get_by_name("tasks", project_id=sample_project["id"]) == override
    assert agents.get(project_agent.id) == project_agent


@pytest.mark.parametrize("value", ["tasks", ["tasks", 7], ["../tasks"]])
def test_malformed_owned_requirements_are_preserved(temp_db: HubDatabase, value: object) -> None:
    defaults = SessionVariableDefaultManager(temp_db)
    row = defaults.create("required_skills", value, tags=["gobby"])
    result = migrate_instruction_requirements(temp_db, load_capability_catalog())
    assert result.updated == 0
    assert any(row.id in error and "preserved" in error for error in result.errors)
    assert defaults.get(row.id) == row


def test_partial_failure_can_retry_without_duplicate_references(temp_db: HubDatabase) -> None:
    catalog = load_capability_catalog()
    agents = AgentDefinitionManager(temp_db)
    row = agents.upsert_with_steps(
        "retry-agent",
        {"name": "retry-agent", "prompts": {"agent": "tasks"}},
        {"variables": {"required_skills": ["tasks", "tasks"]}, "steps": []},
        tags=["gobby"],
    )
    defaults = SessionVariableDefaultManager(temp_db)
    default = defaults.create("required_skills", ["memory"], tags=["gobby"])
    with patch.object(
        AgentDefinitionManager, "set_step_workflow", side_effect=OSError("write failed")
    ):
        failed = migrate_instruction_requirements(temp_db, catalog)
    assert any(row.id in error and "write failed" in error for error in failed.errors)
    assert agents.get(row.id) == row
    assert defaults.get(default.id).default_value == [catalog.folded_skills["memory"]]
    retried = migrate_instruction_requirements(temp_db, catalog)
    assert retried.errors == []
    assert retried.updated == 1
    assert agents.get(row.id).definition_json["step_workflow"]["variables"]["required_skills"] == [
        catalog.folded_skills["tasks"]
    ]
    assert migrate_instruction_requirements(temp_db, catalog).updated == 0


def test_empty_database_migration_is_a_noop(temp_db: HubDatabase) -> None:
    result = migrate_instruction_requirements(temp_db, load_capability_catalog())
    assert result.updated == 0
    assert result.errors == []
    assert result.warnings == []


def test_migration_locks_child_against_concurrent_workflow_edits(temp_db: HubDatabase) -> None:
    from collections.abc import Mapping
    from concurrent.futures import ThreadPoolExecutor

    from psycopg.errors import LockNotAvailable

    from gobby.storage.definitions.agents import AgentDefinitionRow

    agents = AgentDefinitionManager(temp_db)
    row = agents.upsert_with_steps(
        "locked-agent",
        {"name": "locked-agent", "prompts": {"agent": "Instructions"}},
        {"variables": {"required_skills": ["tasks"]}, "steps": []},
        tags=["gobby"],
    )
    write_child = AgentDefinitionManager.set_step_workflow

    def concurrent_edit() -> None:
        with temp_db.transaction() as txn:
            txn.execute("SET LOCAL lock_timeout = '100ms'")
            write_child(agents, row.id, {"variables": {"concurrent": True}, "steps": []})

    def write_with_concurrent_probe(
        manager: AgentDefinitionManager,
        agent_definition_id: str,
        step_workflow: Mapping[str, Any] | None,
    ) -> AgentDefinitionRow:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(concurrent_edit)
            with pytest.raises(LockNotAvailable):
                future.result(timeout=5)
        return write_child(manager, agent_definition_id, step_workflow)

    with patch.object(AgentDefinitionManager, "set_step_workflow", write_with_concurrent_probe):
        result = migrate_instruction_requirements(temp_db, load_capability_catalog())
    assert result.errors == []
    assert result.updated == 1
    assert agents.get(row.id).definition_json["step_workflow"]["variables"] == {
        "required_skills": ["gobby:references/tasks/overview.md"]
    }


def test_runtime_requirements_are_reported_without_rewriting_history(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    from gobby.storage.tasks import LocalTaskManager
    from gobby.utils.machine_id import require_machine_id
    from gobby.workflows.agent_models import AgentStepWorkflowBody
    from gobby.workflows.definitions import WorkflowStep
    from gobby.workflows.state_manager import SessionVariableManager
    from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance

    session = session_manager.register(
        external_id="reference-migration-fixture",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(session.id, {"additional_skills": ["live-session"]})
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id=sample_project["id"],
        title="Keep tasks history",
        additional_skills=["tasks"],
        validation_criteria="Migration preserves this task and reports the retired requirement",
    )
    agent = make_agent_definition(
        name="runtime-fixture",
        prompts={"agent": "Task instructions"},
        step_workflow=AgentStepWorkflowBody(
            variables={"required_skills": ["development-discipline"]},
            steps=[WorkflowStep(name="work")],
        ),
    )
    instances = AgentStepInstanceManager(temp_db)
    instance = build_step_instance(agent, session_id=session.id, step_workflow_id=None)
    instances.save(instance)
    original = instances.get_for_session(session.id)
    result = migrate_instruction_requirements(temp_db, load_capability_catalog())
    assert result.errors == []
    assert result.updated == 0
    assert variables.get_variables(session.id)["additional_skills"] == ["live-session"]
    assert tasks.get_task(task.id) == task
    assert instances.get_for_session(session.id) == original
    assert any(task.id in warning and "tasks ->" in warning for warning in result.warnings)
    assert any(
        session.id in warning and "live-session ->" in warning for warning in result.warnings
    )
    for field in ("variables", "snapshot_json"):
        assert any(
            instance.id in warning and field in warning and "development-discipline ->" in warning
            for warning in result.warnings
        )
