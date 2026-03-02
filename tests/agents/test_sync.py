"""Tests for sync_bundled_agents."""

from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.agents.sync import sync_bundled_agents
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody


def _setup_db(tmp_path: Path) -> LocalDatabase:
    """Create a fresh database with migrations applied."""
    db = LocalDatabase(tmp_path / "test.db")
    run_migrations(db)
    return db


class TestSyncBundledAgents:
    """Tests for sync_bundled_agents function."""

    @pytest.mark.unit
    def test_sync_creates_bundled_agents(self, tmp_path: Path) -> None:
        """Test that sync creates bundled agent definitions in workflow_definitions."""
        db = _setup_db(tmp_path)

        # Create a fake agents directory with one YAML file
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "test-agent.yaml").write_text(
            "name: test-agent\ndescription: A test agent\nprovider: claude\nmode: terminal\n"
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["success"] is True
        assert result["synced"] == 1
        assert result["updated"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == []

        # Verify the agent was created in workflow_definitions
        mgr = LocalWorkflowDefinitionManager(db)
        rows = mgr.list_all(workflow_type="agent")
        row = next((r for r in rows if r.name == "test-agent"), None)
        assert row is not None
        assert row.source == "template"
        body = AgentDefinitionBody.model_validate_json(row.definition_json)
        assert body.name == "test-agent"

    @pytest.mark.unit
    def test_sync_skips_unchanged(self, tmp_path: Path) -> None:
        """Test that sync skips agents that haven't changed."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "test-agent.yaml").write_text(
            "name: test-agent\ndescription: A test agent\nprovider: claude\nmode: terminal\n"
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            # First sync
            result1 = sync_bundled_agents(db)
            assert result1["synced"] == 1

            # Second sync — should skip
            result2 = sync_bundled_agents(db)
            assert result2["synced"] == 0
            assert result2["skipped"] == 1
            assert result2["updated"] == 0

    @pytest.mark.unit
    def test_sync_updates_changed(self, tmp_path: Path) -> None:
        """Test that sync updates agents when content changes."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        yaml_file = agents_dir / "test-agent.yaml"
        yaml_file.write_text(
            "name: test-agent\ndescription: A test agent\nprovider: claude\nmode: terminal\n"
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            # First sync
            sync_bundled_agents(db)

            # Modify the file
            yaml_file.write_text(
                "name: test-agent\ndescription: Updated description\nprovider: claude\nmode: terminal\n"
            )

            # Second sync — should update
            result2 = sync_bundled_agents(db)
            assert result2["updated"] == 1
            assert result2["synced"] == 0
            assert result2["skipped"] == 0

        # Verify updated content
        mgr = LocalWorkflowDefinitionManager(db)
        rows = mgr.list_all(workflow_type="agent")
        row = next((r for r in rows if r.name == "test-agent"), None)
        assert row is not None
        body = AgentDefinitionBody.model_validate_json(row.definition_json)
        assert body.description == "Updated description"

    @pytest.mark.unit
    def test_sync_multiple_agents(self, tmp_path: Path) -> None:
        """Test syncing multiple agent files."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "agent-a.yaml").write_text(
            "name: agent-a\nprovider: claude\nmode: terminal\n"
        )
        (agents_dir / "agent-b.yaml").write_text(
            "name: agent-b\nprovider: gemini\nmode: terminal\n"
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["synced"] == 2
        assert result["errors"] == []

    @pytest.mark.unit
    def test_sync_missing_path(self, tmp_path: Path) -> None:
        """Test sync handles missing agents directory gracefully."""
        db = _setup_db(tmp_path)

        with patch(
            "gobby.agents.sync.get_bundled_agents_path",
            return_value=tmp_path / "nonexistent",
        ):
            result = sync_bundled_agents(db)

        assert result["success"] is True
        assert result["synced"] == 0
        assert len(result["errors"]) == 1

    @pytest.mark.unit
    def test_sync_invalid_yaml(self, tmp_path: Path) -> None:
        """Test sync handles invalid YAML gracefully."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "bad.yaml").write_text("not: valid: yaml: [[[")

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["synced"] == 0
        assert len(result["errors"]) == 1

    @pytest.mark.unit
    def test_sync_propagates_to_installed_copy(self, tmp_path: Path) -> None:
        """Test that sync propagates definition changes to installed copies."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        yaml_file = agents_dir / "test-agent.yaml"
        yaml_file.write_text(
            "name: test-agent\ndescription: Original\nprovider: claude\nmode: terminal\n"
        )

        mgr = LocalWorkflowDefinitionManager(db)

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            # First sync — creates template
            sync_bundled_agents(db)

        # Create an installed copy (mimics install_all_templates)
        template_row = mgr.get_by_name("test-agent", include_templates=True)
        assert template_row is not None
        mgr.install_from_template(template_row.id)

        installed_row = mgr.get_by_name("test-agent")
        assert installed_row is not None
        assert installed_row.source == "installed"
        original_body = AgentDefinitionBody.model_validate_json(installed_row.definition_json)
        assert original_body.description == "Original"

        # Modify the template YAML
        yaml_file.write_text(
            "name: test-agent\ndescription: Updated v2\nprovider: claude\nmode: terminal\n"
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            # Second sync — should update template AND propagate to installed copy
            result = sync_bundled_agents(db)

        assert result["updated"] == 1

        # Verify the installed copy was updated
        installed_row = mgr.get_by_name("test-agent")
        assert installed_row is not None
        assert installed_row.source == "installed"
        updated_body = AgentDefinitionBody.model_validate_json(installed_row.definition_json)
        assert updated_body.description == "Updated v2"

    @pytest.mark.integration
    def test_sync_with_real_bundled_agents(self, tmp_path: Path) -> None:
        """Test that sync works with the actual bundled agents directory."""
        db = _setup_db(tmp_path)

        result = sync_bundled_agents(db)

        assert result["success"] is True
        # At least one bundled agent should be synced (some may be skipped
        # due to name collision with bundled workflows)
        assert result["synced"] + result["skipped"] + result["updated"] >= 1

        # Verify agents are in workflow_definitions
        mgr = LocalWorkflowDefinitionManager(db)
        rows = mgr.list_all(workflow_type="agent")
        assert len(rows) > 0
        names = [r.name for r in rows]
        # Check for agents from the new-format bundled definitions
        assert any(n in names for n in ("default", "claude-cli-sonnet", "gemini-cli"))
