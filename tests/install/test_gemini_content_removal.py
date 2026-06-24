"""Regression tests for retired Gemini bundled content.

The ``gemini-image-gen`` workflow agent and the ``nano-banana`` skill were
removed from disk as part of the Gemini CLI removal. These tests prove the
removal is real (absent from the bundled trees) and that previously-installed
rows soft-delete through the normal bundled sync orphan path rather than via a
direct DB mutation.
"""

import pytest

from gobby.agents.sync import get_bundled_agents_path, sync_bundled_agents
from gobby.skills.sync import get_bundled_skills_path, sync_bundled_skills
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

pytestmark = pytest.mark.integration


def test_gemini_image_gen_yaml_absent_from_bundled_agents() -> None:
    """The retired agent template is gone from the bundled agents tree."""
    agents_path = get_bundled_agents_path()
    names = {path.stem for path in agents_path.rglob("*.yaml")}
    assert "gemini-image-gen" not in names


def test_nano_banana_skill_absent_from_bundled_skills() -> None:
    """The retired skill directory is gone from the bundled skills tree."""
    skills_path = get_bundled_skills_path()
    skill_dirs = {path.name for path in skills_path.iterdir() if path.is_dir()}
    assert "nano-banana" not in skill_dirs


def test_gemini_image_gen_agent_soft_deleted_by_sync(temp_db: HubDatabase) -> None:
    """A stale gemini-image-gen agent row is orphaned by normal agent sync."""
    db = temp_db
    manager = LocalWorkflowDefinitionManager(db)
    manager.create(
        name="gemini-image-gen",
        definition_json='{"name": "gemini-image-gen", "description": "retired"}',
        workflow_type="agent",
        description="retired image-gen agent",
        source="installed",
        enabled=True,
        tags=["gobby"],
    )
    assert manager.get_by_name("gemini-image-gen") is not None

    result = sync_bundled_agents(db)

    assert result["success"] is True
    assert result["orphaned"] >= 1
    # Default get_by_name excludes soft-deleted rows.
    assert manager.get_by_name("gemini-image-gen") is None


def test_nano_banana_skill_soft_deleted_by_sync(temp_db: HubDatabase) -> None:
    """A stale nano-banana skill row is orphaned by normal skill sync."""
    db = temp_db
    storage = LocalSkillManager(db)
    storage.create_skill(
        name="nano-banana",
        description="retired gemini image skill",
        content="# nano-banana\n\nRetired.",
        metadata={"gobby": {"audience": "all"}},
        source="installed",
    )
    installed_names = {
        skill.name for skill in storage.list_skills(project_id=None, include_global=False, limit=-1)
    }
    assert "nano-banana" in installed_names

    result = sync_bundled_skills(db)

    assert result["success"] is True
    assert result["orphaned"] >= 1
    remaining_names = {
        skill.name for skill in storage.list_skills(project_id=None, include_global=False, limit=-1)
    }
    assert "nano-banana" not in remaining_names
