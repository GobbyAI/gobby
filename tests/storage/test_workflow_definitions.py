"""Tests for LocalWorkflowDefinitionManager."""

import json

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import (
    LocalWorkflowDefinitionManager,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    """Create a fresh database with migrations applied."""
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
    """Create a workflow definition manager."""
    return LocalWorkflowDefinitionManager(db)


SAMPLE_DEFINITION = json.dumps(
    {
        "name": "test-workflow",
        "description": "A test workflow",
        "steps": [{"name": "work", "tools": ["all"]}],
    }
)

SAMPLE_PIPELINE_DEFINITION = json.dumps(
    {
        "name": "test-pipeline",
        "type": "pipeline",
        "steps": [{"id": "build", "exec": {"command": "make build"}}],
    }
)

SAMPLE_YAML = """\
name: yaml-workflow
description: Imported from YAML
type: pipeline
version: "2.0"
enabled: true
priority: 50
sources:
  - claude
  - gemini
steps:
  - name: research
    tools: [Read, Grep]
  - name: implement
    tools: [all]
"""

SAMPLE_PIPELINE_YAML = """\
name: yaml-pipeline
description: A pipeline from YAML
type: pipeline
steps:
  - id: build
    exec:
      command: make build
"""


# =============================================================================
# WorkflowDefinitionRow
# =============================================================================


def test_workflow_definition_row_to_dict(manager: LocalWorkflowDefinitionManager) -> None:
    """Test that to_dict() returns all fields."""
    row = manager.create(
        name="test-workflow",
        definition_json=SAMPLE_DEFINITION,
        tags=["dev", "test"],
        sources=["claude"],
    )
    d = row.to_dict()
    assert d["id"] == row.id
    assert d["name"] == "test-workflow"
    assert d["workflow_type"] == "workflow"
    assert d["enabled"] is True
    assert d["priority"] == 100
    assert d["source"] == "installed"
    assert d["sources"] == ["claude"]
    assert d["tags"] == ["dev", "test"]
    assert d["created_at"] is not None
    assert d["updated_at"] is not None


# =============================================================================
# Create
# =============================================================================


def test_create_with_all_fields(manager: LocalWorkflowDefinitionManager) -> None:
    """Test creating a workflow definition with all fields populated."""
    row = manager.create(
        name="full-workflow",
        definition_json=SAMPLE_DEFINITION,
        workflow_type="workflow",
        description="Full description",
        version="2.0",
        enabled=True,
        priority=50,
        sources=["claude", "gemini"],
        canvas_json='{"nodes": [], "edges": []}',
        source="installed",
        tags=["tag1", "tag2"],
    )

    assert row.id is not None
    assert row.name == "full-workflow"
    assert row.workflow_type == "workflow"
    assert row.description == "Full description"
    assert row.version == "2.0"
    assert row.enabled is True
    assert row.priority == 50
    assert row.sources == ["claude", "gemini"]
    assert row.canvas_json is not None
    assert json.loads(row.canvas_json) == {"nodes": [], "edges": []}
    assert row.source == "installed"
    assert row.tags == ["tag1", "tag2"]
    assert row.project_id is None


def test_create_pipeline(manager: LocalWorkflowDefinitionManager) -> None:
    """Test creating a pipeline definition."""
    row = manager.create(
        name="test-pipeline",
        definition_json=SAMPLE_PIPELINE_DEFINITION,
        workflow_type="pipeline",
    )

    assert row.workflow_type == "pipeline"
    assert row.name == "test-pipeline"


def test_create_defaults(manager: LocalWorkflowDefinitionManager) -> None:
    """Test that defaults are applied correctly."""
    row = manager.create(
        name="default-workflow",
        definition_json=SAMPLE_DEFINITION,
    )

    assert row.workflow_type == "workflow"
    assert row.version == "1.0"
    assert row.enabled is True
    assert row.priority == 100
    assert row.source == "installed"
    assert row.project_id is None
    assert row.canvas_json is None
    assert row.sources is None
    assert row.tags is None


# =============================================================================
# Get
# =============================================================================


def test_get_by_id(manager: LocalWorkflowDefinitionManager) -> None:
    """Test retrieving a workflow definition by ID."""
    created = manager.create(name="get-test", definition_json=SAMPLE_DEFINITION)
    fetched = manager.get(created.id)

    assert fetched.id == created.id
    assert fetched.name == "get-test"


def test_get_nonexistent_raises(manager: LocalWorkflowDefinitionManager) -> None:
    """Test that getting a nonexistent definition raises ValueError."""
    with pytest.raises(ValueError, match="not found"):
        manager.get("nonexistent-id")


# =============================================================================
# Get by Name
# =============================================================================


def test_get_by_name_global(manager: LocalWorkflowDefinitionManager) -> None:
    """Test get_by_name finds global (project_id=NULL) definitions."""
    manager.create(name="global-wf", definition_json=SAMPLE_DEFINITION)

    result = manager.get_by_name("global-wf")
    assert result is not None
    assert result.name == "global-wf"
    assert result.project_id is None


def test_get_by_name_project_scoped(
    db: HubDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    """Test get_by_name prefers project-scoped over global."""
    # Create project
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        ("proj-1", "Test Project"),
    )

    # Create global version
    manager.create(name="scoped-wf", definition_json=SAMPLE_DEFINITION, description="global")

    # Create project-scoped version
    manager.create(
        name="scoped-wf",
        definition_json=SAMPLE_DEFINITION,
        project_id="proj-1",
        description="project-scoped",
    )

    # With project_id, should return project-scoped
    result = manager.get_by_name("scoped-wf", project_id="proj-1")
    assert result is not None
    assert result.description == "project-scoped"
    assert result.project_id == "proj-1"


def test_get_by_name_fallback_to_global(
    db: HubDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    """Test get_by_name falls back to global when no project-scoped match."""
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        ("proj-1", "Test Project"),
    )

    manager.create(name="fallback-wf", definition_json=SAMPLE_DEFINITION, description="global")

    result = manager.get_by_name("fallback-wf", project_id="proj-1")
    assert result is not None
    assert result.description == "global"
    assert result.project_id is None


def test_get_by_name_not_found(manager: LocalWorkflowDefinitionManager) -> None:
    """Test get_by_name returns None when not found."""
    result = manager.get_by_name("nonexistent")
    assert result is None


def test_get_by_name_returns_installed_rows(
    manager: LocalWorkflowDefinitionManager,
) -> None:
    """get_by_name should return installed rows."""
    manager.create(
        name="installed-rule",
        definition_json=SAMPLE_DEFINITION,
        source="installed",
    )

    result = manager.get_by_name("installed-rule")
    assert result is not None
    assert result.source == "installed"


# =============================================================================
# Update
# =============================================================================


def test_update_fields(manager: LocalWorkflowDefinitionManager) -> None:
    """Test updating specific fields."""
    created = manager.create(name="update-test", definition_json=SAMPLE_DEFINITION)

    updated = manager.update(
        created.id,
        description="Updated description",
        priority=25,
        enabled=False,
    )

    assert updated.description == "Updated description"
    assert updated.priority == 25
    assert updated.enabled is False
    assert updated.updated_at != created.updated_at


def test_update_json_fields(manager: LocalWorkflowDefinitionManager) -> None:
    """Test updating JSON fields (sources, tags)."""
    created = manager.create(name="json-update", definition_json=SAMPLE_DEFINITION)

    updated = manager.update(
        created.id,
        sources=["claude", "gemini"],
        tags=["production"],
    )

    assert updated.sources == ["claude", "gemini"]
    assert updated.tags == ["production"]


def test_update_no_fields(manager: LocalWorkflowDefinitionManager) -> None:
    """Test that updating with no fields returns the existing row unchanged."""
    created = manager.create(name="no-update", definition_json=SAMPLE_DEFINITION)
    result = manager.update(created.id)
    assert result.id == created.id


# =============================================================================
# Delete
# =============================================================================


def test_delete(manager: LocalWorkflowDefinitionManager) -> None:
    """Test deleting a workflow definition."""
    created = manager.create(name="delete-test", definition_json=SAMPLE_DEFINITION)

    assert manager.delete(created.id) is True

    with pytest.raises(ValueError):
        manager.get(created.id)


def test_delete_nonexistent(manager: LocalWorkflowDefinitionManager) -> None:
    """Test deleting a nonexistent definition returns False."""
    assert manager.delete("nonexistent-id") is False


# =============================================================================
# List All
# =============================================================================


def test_list_all(manager: LocalWorkflowDefinitionManager) -> None:
    """Test listing all definitions (includes bundled + custom)."""
    # Bundled workflows already exist from migration
    initial = manager.list_all()
    initial_count = len(initial)

    manager.create(name="custom-1", definition_json=SAMPLE_DEFINITION)
    manager.create(name="custom-2", definition_json=SAMPLE_DEFINITION, workflow_type="pipeline")

    all_defs = manager.list_all()
    assert len(all_defs) == initial_count + 2


def test_list_all_filter_workflow_type(manager: LocalWorkflowDefinitionManager) -> None:
    """Test listing definitions filtered by workflow_type."""
    manager.create(name="filter-wf", definition_json=SAMPLE_DEFINITION, workflow_type="workflow")
    manager.create(
        name="filter-pipe", definition_json=SAMPLE_PIPELINE_DEFINITION, workflow_type="pipeline"
    )

    workflows = manager.list_all(workflow_type="workflow")
    pipelines = manager.list_all(workflow_type="pipeline")

    wf_names = {w.name for w in workflows}
    pipe_names = {p.name for p in pipelines}

    assert "filter-wf" in wf_names
    assert "filter-pipe" not in wf_names
    assert "filter-pipe" in pipe_names
    assert "filter-wf" not in pipe_names


def test_list_all_filter_enabled(manager: LocalWorkflowDefinitionManager) -> None:
    """Test listing definitions filtered by enabled status."""
    manager.create(name="enabled-wf", definition_json=SAMPLE_DEFINITION, enabled=True)
    manager.create(name="disabled-wf", definition_json=SAMPLE_DEFINITION, enabled=False)

    enabled = manager.list_all(enabled=True)
    disabled = manager.list_all(enabled=False)

    enabled_names = {w.name for w in enabled}
    disabled_names = {w.name for w in disabled}

    assert "enabled-wf" in enabled_names
    assert "disabled-wf" not in enabled_names
    assert "disabled-wf" in disabled_names


def test_list_all_filter_project(db: HubDatabase, manager: LocalWorkflowDefinitionManager) -> None:
    """Test listing definitions filtered by project_id (includes global)."""
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        ("proj-1", "Test Project"),
    )

    # Create a global workflow (project_id=NULL)
    manager.create(name="global-wf", definition_json=SAMPLE_DEFINITION)
    manager.create(name="proj-wf", definition_json=SAMPLE_DEFINITION, project_id="proj-1")

    results = manager.list_all(project_id="proj-1")
    names = {w.name for w in results}

    # Should include project-scoped AND global
    assert "proj-wf" in names
    assert "global-wf" in names
    assert any(w.project_id is None for w in results)


# =============================================================================
# Import from YAML
# =============================================================================


def test_import_from_yaml(manager: LocalWorkflowDefinitionManager) -> None:
    """Test importing a workflow definition from YAML content."""
    row = manager.import_from_yaml(SAMPLE_YAML)

    assert row.name == "yaml-workflow"
    assert row.description.strip() == "Imported from YAML"
    assert row.workflow_type == "pipeline"
    assert row.version == "2.0"
    assert row.enabled is True
    assert row.priority == 50
    assert row.sources == ["claude", "gemini"]
    assert row.source == "installed"

    # Verify definition_json round-trips
    data = json.loads(row.definition_json)
    assert data["name"] == "yaml-workflow"
    assert data["steps"][0]["name"] == "research"


def test_import_from_yaml_pipeline(manager: LocalWorkflowDefinitionManager) -> None:
    """Test importing a pipeline from YAML."""
    row = manager.import_from_yaml(SAMPLE_PIPELINE_YAML)

    assert row.name == "yaml-pipeline"
    assert row.workflow_type == "pipeline"
    assert row.source == "installed"


def test_import_from_yaml_invalid(manager: LocalWorkflowDefinitionManager) -> None:
    """Test importing invalid YAML raises ValueError."""
    with pytest.raises(ValueError, match="Invalid workflow YAML"):
        manager.import_from_yaml("not_a_dict: [1, 2, 3]")


def test_import_from_yaml_step_type_rejected(
    manager: LocalWorkflowDefinitionManager,
) -> None:
    """`type: step` is not a valid top-level YAML import type. Step workflows are
    synthesized by spawn_agent, never hand-imported. Regression for the legacy
    map that used to silently rewrite step → workflow_type='pipeline'."""
    yaml_with_step = """
name: rogue-step
type: step
steps:
  - name: claim
"""
    with pytest.raises(ValueError, match="Invalid or missing 'type'"):
        manager.import_from_yaml(yaml_with_step)


def test_import_from_yaml_workflow_type_rejected(
    manager: LocalWorkflowDefinitionManager,
) -> None:
    """`type: workflow` was the other half of the legacy alias map; reject it."""
    yaml_with_workflow = """
name: rogue-workflow
type: workflow
steps:
  - name: do
"""
    with pytest.raises(ValueError, match="Invalid or missing 'type'"):
        manager.import_from_yaml(yaml_with_workflow)


def test_import_from_yaml_missing_type_rejected(
    manager: LocalWorkflowDefinitionManager,
) -> None:
    """Missing `type:` used to silently fall back to pipeline; now it errors."""
    yaml_no_type = "name: anonymous\nsteps:\n  - name: do\n"
    with pytest.raises(ValueError, match="Invalid or missing 'type'"):
        manager.import_from_yaml(yaml_no_type)


# =============================================================================
# Export to YAML
# =============================================================================


def test_export_to_yaml(manager: LocalWorkflowDefinitionManager) -> None:
    """Test exporting a workflow definition as YAML."""
    created = manager.create(
        name="export-test",
        definition_json=json.dumps({"name": "export-test", "steps": []}),
    )

    yaml_output = manager.export_to_yaml(created.id)

    assert "name: export-test" in yaml_output
    assert isinstance(yaml_output, str)


# =============================================================================
# Duplicate
# =============================================================================


def test_duplicate(manager: LocalWorkflowDefinitionManager) -> None:
    """Test duplicating a workflow definition with a new name."""
    original = manager.create(
        name="original",
        definition_json=SAMPLE_DEFINITION,
        description="Original description",
        priority=25,
        tags=["production"],
        sources=["claude"],
    )

    duplicate = manager.duplicate(original.id, "copy-of-original")

    assert duplicate.id != original.id
    assert duplicate.name == "copy-of-original"
    assert duplicate.description == original.description
    assert duplicate.priority == original.priority
    assert duplicate.workflow_type == original.workflow_type
    assert duplicate.tags == original.tags
    assert duplicate.sources == original.sources
    assert duplicate.source == "installed"

    # Verify definition_json has updated name
    data = json.loads(duplicate.definition_json)
    assert data["name"] == "copy-of-original"


def test_duplicate_nonexistent_raises(manager: LocalWorkflowDefinitionManager) -> None:
    """Test that duplicating a nonexistent definition raises ValueError."""
    with pytest.raises(ValueError, match="not found"):
        manager.duplicate("nonexistent-id", "new-name")


# =============================================================================
# Install from Template
# =============================================================================


# =============================================================================
# Move to Project / Move to Global
# =============================================================================


def test_move_to_project(db: HubDatabase, manager: LocalWorkflowDefinitionManager) -> None:
    """Test moving an installed definition to project scope."""
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        ("proj-1", "Test Project"),
    )

    row = manager.create(name="move-test", definition_json=SAMPLE_DEFINITION)
    assert row.source == "installed"
    assert row.project_id is None

    moved = manager.move_to_project(row.id, "proj-1")
    assert moved.source == "project"
    assert moved.project_id == "proj-1"


def test_move_to_global(db: HubDatabase, manager: LocalWorkflowDefinitionManager) -> None:
    """Test moving a project-scoped definition to global scope."""
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        ("proj-1", "Test Project"),
    )

    row = manager.create(
        name="move-global-test",
        definition_json=SAMPLE_DEFINITION,
        source="project",
        project_id="proj-1",
    )
    assert row.source == "project"
    assert row.project_id == "proj-1"

    moved = manager.move_to_global(row.id)
    assert moved.source == "installed"
    assert moved.project_id is None
