"""Tests for skills/__init__.py — targeting uncovered lines."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill(
    *,
    id: str = "skill-1",
    name: str = "test-skill",
    description: str = "A test skill",
    content: str = "# Skill Content",
    version: str = "1.0",
    license: str | None = None,
    compatibility: dict | None = None,
    allowed_tools: list | None = None,
    metadata: dict | None = None,
    enabled: bool = True,
    source: str = "installed",
    source_path: str | None = None,
    source_type: str | None = None,
    source_ref: str | None = None,
    project_id: str | None = None,
    deleted_at: str | None = None,
) -> MagicMock:
    skill = MagicMock()
    skill.id = id
    skill.name = name
    skill.description = description
    skill.content = content
    skill.version = version
    skill.license = license
    skill.compatibility = compatibility
    skill.allowed_tools = allowed_tools
    skill.metadata = metadata or {}
    skill.enabled = enabled
    skill.source = source
    skill.source_path = source_path
    skill.source_type = source_type
    skill.source_ref = source_ref
    skill.project_id = project_id
    skill.deleted_at = deleted_at
    return skill


@pytest.fixture
def mock_db() -> MagicMock:
    return MagicMock()


def _create_registry(db: Any, project_id: str | None = None, hub_manager: Any | None = None) -> Any:
    """Create skills registry with mocked storage."""
    with (
        patch("gobby.mcp_proxy.tools.skills.LocalSkillManager") as MockStorage,
        patch("gobby.mcp_proxy.tools.skills.SkillSearch") as MockSearch,
        patch("gobby.mcp_proxy.tools.skills.SkillUpdater") as MockUpdater,
        patch("gobby.mcp_proxy.tools.skills.SkillLoader") as MockLoader,
        patch("gobby.mcp_proxy.tools.skills.LocalSessionManager") as MockSM,
        patch("gobby.mcp_proxy.tools.skills.SkillChangeNotifier") as MockNotifier,
    ):
        mock_storage = MagicMock()
        mock_storage.list_skills.return_value = []
        MockStorage.return_value = mock_storage

        mock_search = MagicMock()
        mock_search.search_async = AsyncMock(return_value=[])
        MockSearch.return_value = mock_search

        mock_updater = MagicMock()
        MockUpdater.return_value = mock_updater

        mock_loader = MagicMock()
        MockLoader.return_value = mock_loader

        mock_sm = MagicMock()
        MockSM.return_value = mock_sm

        mock_notifier = MagicMock()
        MockNotifier.return_value = mock_notifier

        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(db, project_id=project_id, hub_manager=hub_manager)

        # Expose mocks for assertions
        registry._mock_storage = mock_storage
        registry._mock_search = mock_search
        registry._mock_updater = mock_updater
        registry._mock_loader = mock_loader
        registry._mock_sm = mock_sm

        return registry


# ---------------------------------------------------------------------------
# list_skills tests
# ---------------------------------------------------------------------------


class TestListSkills:
    """Tests for list_skills tool."""

    @pytest.mark.asyncio
    async def test_list_skills_empty(self, mock_db):
        registry = _create_registry(mock_db)
        result = await registry.call("list_skills", {})
        assert result["success"] is True
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_list_skills_with_metadata(self, mock_db):
        skill = _make_skill(
            metadata={"skillport": {"category": "dev", "tags": ["testing"]}},
        )
        registry = _create_registry(mock_db)
        registry._mock_storage.list_skills.return_value = [skill]

        result = await registry.call("list_skills", {})
        assert result["success"] is True
        assert result["count"] == 1
        assert result["skills"][0]["category"] == "dev"
        assert result["skills"][0]["tags"] == ["testing"]

    @pytest.mark.asyncio
    async def test_list_skills_with_category_filter(self, mock_db):
        registry = _create_registry(mock_db)
        result = await registry.call("list_skills", {"category": "dev"})
        assert result["success"] is True
        # list_skills is called during registry creation + during the tool call
        last_call_kwargs = registry._mock_storage.list_skills.call_args.kwargs
        assert last_call_kwargs.get("category") == "dev"

    @pytest.mark.asyncio
    async def test_list_skills_exception(self, mock_db):
        registry = _create_registry(mock_db)
        registry._mock_storage.list_skills.side_effect = RuntimeError("db error")

        result = await registry.call("list_skills", {})
        assert result["success"] is False
        assert "db error" in result["error"]


# ---------------------------------------------------------------------------
# get_skill tests
# ---------------------------------------------------------------------------


class TestGetSkill:
    """Tests for get_skill tool."""

    @pytest.mark.asyncio
    async def test_get_skill_no_params(self, mock_db):
        registry = _create_registry(mock_db)
        result = await registry.call("get_skill", {})
        assert result["success"] is False
        assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_get_skill_by_name(self, mock_db):
        skill = _make_skill()
        registry = _create_registry(mock_db)
        registry._mock_storage.get_by_name.return_value = skill

        result = await registry.call("get_skill", {"name": "test-skill"})
        assert result["success"] is True
        assert result["skill"]["name"] == "test-skill"

    @pytest.mark.asyncio
    async def test_get_skill_by_id(self, mock_db):
        skill = _make_skill()
        registry = _create_registry(mock_db)
        registry._mock_storage.get_skill.return_value = skill

        result = await registry.call("get_skill", {"skill_id": "skill-1"})
        assert result["success"] is True
        assert result["skill"]["name"] == "test-skill"

    @pytest.mark.asyncio
    async def test_get_skill_not_found(self, mock_db):
        registry = _create_registry(mock_db)
        registry._mock_storage.get_skill.side_effect = ValueError("not found")
        registry._mock_storage.get_by_name.return_value = None

        result = await registry.call("get_skill", {"name": "nope"})
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_get_skill_records_usage(self, mock_db):
        skill = _make_skill()
        registry = _create_registry(mock_db)
        registry._mock_storage.get_by_name.return_value = skill
        registry._mock_sm.resolve_session_reference.return_value = "sess-uuid"

        result = await registry.call("get_skill", {"name": "test-skill", "session_id": "#1"})
        assert result["success"] is True
        registry._mock_sm.record_skills_used.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_skill_exception(self, mock_db):
        registry = _create_registry(mock_db)
        registry._mock_storage.get_skill.side_effect = RuntimeError("db crash")
        registry._mock_storage.get_by_name.side_effect = RuntimeError("db crash")

        result = await registry.call("get_skill", {"name": "x"})
        assert result["success"] is False


# ---------------------------------------------------------------------------
# search_skills tests
# ---------------------------------------------------------------------------


class TestSearchSkills:
    """Tests for search_skills tool."""

    @pytest.mark.asyncio
    async def test_search_empty_query(self, mock_db):
        registry = _create_registry(mock_db)
        result = await registry.call("search_skills", {"query": ""})
        assert result["success"] is False
        assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_search_success(self, mock_db):
        registry = _create_registry(mock_db)

        mock_result = MagicMock()
        mock_result.skill_id = "skill-1"
        mock_result.skill_name = "test-skill"
        mock_result.similarity = 0.95
        registry._mock_search.search_async.return_value = [mock_result]

        skill = _make_skill(metadata={"skillport": {"category": "dev", "tags": ["t"]}})
        registry._mock_storage.get_skill.return_value = skill

        result = await registry.call("search_skills", {"query": "test"})
        assert result["success"] is True
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_search_with_filters(self, mock_db):
        registry = _create_registry(mock_db)

        result = await registry.call(
            "search_skills",
            {"query": "test", "category": "dev", "tags_any": ["t1"]},
        )
        assert result["success"] is True
        registry._mock_search.search_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_exception(self, mock_db):
        registry = _create_registry(mock_db)
        registry._mock_search.search_async.side_effect = RuntimeError("search fail")

        result = await registry.call("search_skills", {"query": "test"})
        assert result["success"] is False


# ---------------------------------------------------------------------------
# remove_skill tests
# ---------------------------------------------------------------------------


class TestRemoveSkill:
    """Tests for remove_skill tool."""

    @pytest.mark.asyncio
    async def test_remove_no_params(self, mock_db):
        registry = _create_registry(mock_db)
        result = await registry.call("remove_skill", {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_remove_by_name(self, mock_db):
        skill = _make_skill()
        registry = _create_registry(mock_db)
        registry._mock_storage.get_by_name.return_value = skill

        result = await registry.call("remove_skill", {"name": "test-skill"})
        assert result["success"] is True
        assert result["removed"] is True

    @pytest.mark.asyncio
    async def test_remove_not_found(self, mock_db):
        registry = _create_registry(mock_db)
        registry._mock_storage.get_skill.side_effect = ValueError("x")
        registry._mock_storage.get_by_name.return_value = None

        result = await registry.call("remove_skill", {"name": "nope"})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_remove_exception(self, mock_db):
        registry = _create_registry(mock_db)
        registry._mock_storage.get_by_name.side_effect = RuntimeError("fail")

        result = await registry.call("remove_skill", {"name": "x"})
        assert result["success"] is False


# ---------------------------------------------------------------------------
# install_from_template tests
# ---------------------------------------------------------------------------


class TestInstallFromTemplate:
    """Tests for install_from_template tool."""

    @pytest.mark.asyncio
    async def test_install_no_params(self, mock_db):
        registry = _create_registry(mock_db)
        result = await registry.call("install_from_template", {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_install_by_name(self, mock_db):
        template = _make_skill(source="template")
        installed = _make_skill(id="installed-1", source="installed")
        registry = _create_registry(mock_db)
        registry._mock_storage.get_by_name.return_value = template
        registry._mock_storage.install_from_template.return_value = installed

        result = await registry.call("install_from_template", {"name": "test-skill"})
        assert result["success"] is True
        assert result["installed"] is True

    @pytest.mark.asyncio
    async def test_install_not_found(self, mock_db):
        registry = _create_registry(mock_db)
        registry._mock_storage.get_skill.side_effect = ValueError("x")
        registry._mock_storage.get_by_name.return_value = None

        result = await registry.call("install_from_template", {"name": "nope"})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_install_exception(self, mock_db):
        registry = _create_registry(mock_db)
        registry._mock_storage.get_by_name.side_effect = RuntimeError("db crash")

        result = await registry.call("install_from_template", {"name": "test-skill"})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_install_template_exception(self, mock_db):
        template = _make_skill(source="template")
        registry = _create_registry(mock_db)
        registry._mock_storage.get_by_name.return_value = template
        registry._mock_storage.install_from_template.side_effect = Exception("failed payload")

        result = await registry.call("install_from_template", {"name": "test-skill"})
        assert result["success"] is False


# ---------------------------------------------------------------------------
# restore_skill tests
# ---------------------------------------------------------------------------


class TestRestoreSkill:
    """Tests for restore_skill tool."""

    @pytest.mark.asyncio
    async def test_restore_no_params(self, mock_db):
        registry = _create_registry(mock_db)
        result = await registry.call("restore_skill", {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_restore_success(self, mock_db):
        skill = _make_skill(deleted_at="2024-01-01")
        restored = _make_skill(deleted_at=None)
        registry = _create_registry(mock_db)
        registry._mock_storage.get_by_name.return_value = skill
        registry._mock_storage.restore.return_value = restored

        result = await registry.call("restore_skill", {"name": "test-skill"})
        assert result["success"] is True
        assert result["restored"] is True

    @pytest.mark.asyncio
    async def test_restore_not_deleted(self, mock_db):
        skill = _make_skill(deleted_at=None)
        registry = _create_registry(mock_db)
        registry._mock_storage.get_by_name.return_value = skill

        result = await registry.call("restore_skill", {"name": "test-skill"})
        assert result["success"] is False
        assert "not deleted" in result["error"]


# ---------------------------------------------------------------------------
# move_skill_to_project / move_skill_to_installed tests
# ---------------------------------------------------------------------------


class TestMoveSkill:
    """Tests for move skill tools."""

    @pytest.mark.asyncio
    async def test_move_to_project(self, mock_db):
        skill = _make_skill(source="project", project_id="proj-1")
        registry = _create_registry(mock_db)
        registry._mock_storage.move_to_project.return_value = skill

        result = await registry.call(
            "move_skill_to_project",
            {"skill_id": "skill-1", "target_project_id": "proj-1"},
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_move_to_project_error(self, mock_db):
        registry = _create_registry(mock_db)
        registry._mock_storage.move_to_project.side_effect = ValueError("nope")

        result = await registry.call(
            "move_skill_to_project",
            {"skill_id": "x", "target_project_id": "p"},
        )
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_move_to_installed(self, mock_db):
        skill = _make_skill(source="installed")
        registry = _create_registry(mock_db)
        registry._mock_storage.move_to_installed.return_value = skill

        result = await registry.call("move_skill_to_installed", {"skill_id": "skill-1"})
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_move_to_installed_error(self, mock_db):
        registry = _create_registry(mock_db)
        registry._mock_storage.move_to_installed.side_effect = ValueError("nope")

        result = await registry.call("move_skill_to_installed", {"skill_id": "x"})
        assert result["success"] is False


# ---------------------------------------------------------------------------
# update_skill tests
# ---------------------------------------------------------------------------


class TestUpdateSkill:
    """Tests for update_skill tool."""

    @pytest.mark.asyncio
    async def test_update_no_params(self, mock_db):
        registry = _create_registry(mock_db)
        result = await registry.call("update_skill", {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_update_success(self, mock_db):
        skill = _make_skill()
        update_result = MagicMock()
        update_result.error = None
        update_result.updated = True
        update_result.skipped = False
        update_result.skip_reason = None

        registry = _create_registry(mock_db)
        registry._mock_storage.get_by_name.return_value = skill
        registry._mock_updater.update_skill.return_value = update_result

        result = await registry.call("update_skill", {"name": "test-skill"})
        assert result["success"] is True
        assert result["updated"] is True

    @pytest.mark.asyncio
    async def test_update_with_error(self, mock_db):
        skill = _make_skill()
        update_result = MagicMock()
        update_result.error = "source not available"

        registry = _create_registry(mock_db)
        registry._mock_storage.get_by_name.return_value = skill
        registry._mock_updater.update_skill.return_value = update_result

        result = await registry.call("update_skill", {"name": "test-skill"})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_update_not_found(self, mock_db):
        registry = _create_registry(mock_db)
        registry._mock_storage.get_skill.side_effect = ValueError("x")
        registry._mock_storage.get_by_name.return_value = None

        result = await registry.call("update_skill", {"name": "nope"})
        assert result["success"] is False


# ---------------------------------------------------------------------------
# list_hubs / search_hub tests
# ---------------------------------------------------------------------------


class TestHubTools:
    """Tests for list_hubs and search_hub tools."""

    @pytest.mark.asyncio
    async def test_list_hubs_no_manager(self, mock_db):
        registry = _create_registry(mock_db, hub_manager=None)
        result = await registry.call("list_hubs", {})
        assert result["success"] is True
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_list_hubs_with_manager(self, mock_db):
        hub_manager = MagicMock()
        hub_manager.list_hubs.return_value = ["hub1"]
        config = MagicMock()
        config.type = "api"
        config.base_url = "https://hub1.example.com"
        hub_manager.get_config.return_value = config

        registry = _create_registry(mock_db, hub_manager=hub_manager)
        result = await registry.call("list_hubs", {})
        assert result["success"] is True
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_search_hub_no_manager(self, mock_db):
        registry = _create_registry(mock_db, hub_manager=None)
        result = await registry.call("search_hub", {"query": "test"})
        assert result["success"] is False
        assert "No hub manager" in result["error"]

    @pytest.mark.asyncio
    async def test_search_hub_empty_query(self, mock_db):
        hub_manager = MagicMock()
        registry = _create_registry(mock_db, hub_manager=hub_manager)
        result = await registry.call("search_hub", {"query": ""})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_search_hub_success(self, mock_db):
        hub_manager = MagicMock()
        hub_manager.search_all = AsyncMock(return_value=[{"name": "skill-1"}])

        registry = _create_registry(mock_db, hub_manager=hub_manager)
        result = await registry.call("search_hub", {"query": "test"})
        assert result["success"] is True
        assert result["count"] == 1
