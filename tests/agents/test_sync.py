"""Tests for sync_bundled_agents."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

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
        """Test that sync creates installed agent definitions directly."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "test-agent.yaml").write_text(
            "name: test-agent\ndescription: A test agent\nprovider: claude\nmode: interactive\n"
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["success"] is True
        assert result["synced"] == 1
        assert result["updated"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == []

        # Verify the agent was created as installed (not template)
        mgr = LocalWorkflowDefinitionManager(db)
        rows = mgr.list_all(workflow_type="agent")
        row = next((r for r in rows if r.name == "test-agent"), None)
        assert row is not None
        assert row.source == "installed"
        body = AgentDefinitionBody.model_validate_json(row.definition_json)
        assert body.name == "test-agent"

    @pytest.mark.unit
    def test_sync_skips_unchanged(self, tmp_path: Path) -> None:
        """Test that sync skips agents that already exist."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "test-agent.yaml").write_text(
            "name: test-agent\ndescription: A test agent\nprovider: claude\nmode: interactive\n"
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
    def test_sync_uses_filename_when_yaml_name_is_null(self, tmp_path: Path) -> None:
        """A null name should not become a managed orphan key."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "filename-agent.yaml").write_text(
            "name: null\ndescription: From filename\nprovider: claude\nmode: interactive\n"
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["success"] is True
        assert result["synced"] == 1
        row = LocalWorkflowDefinitionManager(db).get_by_name("filename-agent")
        assert row is not None

    @pytest.mark.unit
    def test_sync_updates_existing_installed_definition(self, tmp_path: Path) -> None:
        """Installed bundled agents should update when the template definition changes."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        yaml_file = agents_dir / "test-agent.yaml"
        yaml_file.write_text(
            "name: test-agent\ndescription: A test agent\nprovider: claude\nmode: interactive\n"
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            # First sync
            sync_bundled_agents(db)

            # Modify the file
            yaml_file.write_text(
                "name: test-agent\ndescription: Updated description\nprovider: claude\nmode: interactive\n"
            )

            # Second sync — should update the installed row
            result2 = sync_bundled_agents(db)
            assert result2["skipped"] == 0
            assert result2["synced"] == 0
            assert result2["updated"] == 1

        # Verify content was updated in place
        mgr = LocalWorkflowDefinitionManager(db)
        rows = mgr.list_all(workflow_type="agent")
        row = next((r for r in rows if r.name == "test-agent"), None)
        assert row is not None
        body = AgentDefinitionBody.model_validate_json(row.definition_json)
        assert body.description == "Updated description"

    @pytest.mark.unit
    def test_sync_repairs_stale_generated_step_workflow_for_unchanged_agent(
        self,
        tmp_path: Path,
    ) -> None:
        """Agent sync should refresh stale `<agent>-steps` rows even when the agent row skips."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_yaml = (
            "name: merge-helper\n"
            "description: Merge helper\n"
            "provider: claude\n"
            "mode: interactive\n"
            "steps:\n"
            "  - name: merge\n"
            "    allowed_tools:\n"
            "      - mcp__gobby__call_tool\n"
            "    allowed_mcp_tools:\n"
            "      - gobby-worktrees:get_worktree\n"
            "      - gobby-merge:inspect_merge_state\n"
        )
        (agents_dir / "merge-helper.yaml").write_text(agent_yaml)

        body = AgentDefinitionBody.model_validate(yaml.safe_load(agent_yaml))
        mgr = LocalWorkflowDefinitionManager(db)
        mgr.create(
            name="merge-helper",
            definition_json=body.model_dump_json(),
            workflow_type="agent",
            description=body.description,
            source="installed",
            enabled=body.enabled,
            tags=["gobby"],
        )
        mgr.create(
            name="merge-helper-steps",
            definition_json=json.dumps(
                {
                    "name": "merge-helper-steps",
                    "type": "step",
                    "version": "2.0",
                    "enabled": False,
                    "steps": [
                        {
                            "name": "merge",
                            "allowed_tools": ["mcp__gobby__call_tool"],
                            "allowed_mcp_tools": ["gobby-worktrees:merge_worktree"],
                        }
                    ],
                    "variables": {},
                    "exit_condition": None,
                }
            ),
            workflow_type="workflow",
            source="agent",
            enabled=False,
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["skipped"] == 1
        step_row = mgr.get_by_name("merge-helper-steps")
        assert step_row is not None
        step_body = json.loads(step_row.definition_json)
        allowed = step_body["steps"][0]["allowed_mcp_tools"]
        assert allowed == [
            "gobby-worktrees:get_worktree",
            "gobby-merge:inspect_merge_state",
        ]

    @pytest.mark.unit
    def test_sync_enables_legacy_discovery_placeholder(self, tmp_path: Path) -> None:
        """Old disabled discovery placeholders should become enabled real agents on upgrade."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        yaml_file = agents_dir / "analyst.yaml"
        yaml_file.write_text(
            "name: analyst\n"
            "description: PLACEHOLDER ideation agent\n"
            "enabled: false\n"
            "provider: claude\n"
            "model: haiku\n"
            "instructions: |\n"
            "  PLACEHOLDER\n"
            "  placeholder_agent:analyst:not_implemented\n"
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            sync_bundled_agents(db)

            yaml_file.write_text(
                "name: analyst\n"
                "description: Discovery analyst\n"
                "enabled: true\n"
                "provider: codex\n"
                "model: gpt-5.5\n"
                "reasoning_effort: high\n"
                "instructions: Real ideation agent\n"
            )
            result = sync_bundled_agents(db)

        assert result["updated"] == 1
        mgr = LocalWorkflowDefinitionManager(db)
        row = mgr.get_by_name("analyst")
        assert row is not None
        assert row.enabled is True
        body = AgentDefinitionBody.model_validate_json(row.definition_json)
        assert body.enabled is True
        assert body.provider == "codex"

    @pytest.mark.unit
    def test_sync_preserves_user_disabled_non_placeholder_agent(self, tmp_path: Path) -> None:
        """Template updates should not re-enable unrelated user-disabled bundled agents."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        yaml_file = agents_dir / "test-agent.yaml"
        yaml_file.write_text(
            "name: test-agent\n"
            "description: A test agent\n"
            "enabled: true\n"
            "provider: claude\n"
            "mode: interactive\n"
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            sync_bundled_agents(db)
            mgr = LocalWorkflowDefinitionManager(db)
            row = mgr.get_by_name("test-agent")
            assert row is not None
            mgr.update(row.id, enabled=False)

            yaml_file.write_text(
                "name: test-agent\n"
                "description: Updated description\n"
                "enabled: true\n"
                "provider: claude\n"
                "mode: interactive\n"
            )
            result = sync_bundled_agents(db)

        assert result["updated"] == 1
        row = LocalWorkflowDefinitionManager(db).get_by_name("test-agent")
        assert row is not None
        assert row.enabled is False

    @pytest.mark.unit
    def test_sync_updates_legacy_template_agent_row(self, tmp_path: Path) -> None:
        """Old gobby template rows should become installed bundled rows."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "developer.yaml").write_text(
            "name: developer\n"
            "description: Active developer\n"
            "enabled: true\n"
            "provider: codex\n"
            "mode: interactive\n"
            "instructions: Build features\n"
        )

        mgr = LocalWorkflowDefinitionManager(db)
        mgr.create(
            name="developer",
            definition_json=json.dumps(
                {
                    "name": "developer",
                    "description": "Deprecated developer",
                    "enabled": False,
                    "deprecated": True,
                    "provider": "codex",
                    "mode": "interactive",
                    "instructions": "Deprecated",
                }
            ),
            workflow_type="agent",
            source="template",
            enabled=False,
            tags=["gobby"],
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["updated"] == 1
        row = mgr.get_by_name("developer")
        assert row is not None
        assert row.source == "installed"
        assert row.enabled is True
        body = AgentDefinitionBody.model_validate_json(row.definition_json)
        assert body.enabled is True

    @pytest.mark.unit
    def test_sync_restores_reintroduced_bundled_agent(self, tmp_path: Path) -> None:
        """A changed bundled agent can return after a prior bundled orphan delete."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "developer.yaml").write_text(
            "name: developer\n"
            "description: Active developer\n"
            "enabled: true\n"
            "provider: codex\n"
            "mode: interactive\n"
            "instructions: Build features\n"
        )

        mgr = LocalWorkflowDefinitionManager(db)
        row = mgr.create(
            name="developer",
            definition_json=json.dumps(
                {
                    "name": "developer",
                    "description": "Deprecated developer",
                    "enabled": False,
                    "deprecated": True,
                    "provider": "codex",
                    "mode": "interactive",
                    "instructions": "Deprecated",
                }
            ),
            workflow_type="agent",
            source="installed",
            enabled=False,
            tags=["gobby"],
        )
        mgr.delete(row.id)

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["updated"] == 1
        restored = mgr.get_by_name("developer")
        assert restored is not None
        assert restored.enabled is True
        body = AgentDefinitionBody.model_validate_json(restored.definition_json)
        assert body.description == "Active developer"

    @pytest.mark.unit
    def test_sync_multiple_agents(self, tmp_path: Path) -> None:
        """Test syncing multiple agent files."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "agent-a.yaml").write_text(
            "name: agent-a\nprovider: claude\nmode: interactive\n"
        )
        (agents_dir / "agent-b.yaml").write_text(
            "name: agent-b\nprovider: gemini\nmode: interactive\n"
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
    def test_sync_ignores_deprecated_directory(self, tmp_path: Path) -> None:
        """Deprecated bundled agents are archival and not active install inputs."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        deprecated_dir = agents_dir / "deprecated"
        deprecated_dir.mkdir(parents=True)
        (deprecated_dir / "old-agent.yaml").write_text(
            "name: old-agent\ndescription: Deprecated agent\nmode: interactive\n"
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["success"] is True
        assert result["synced"] == 0
        assert result["updated"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == []

        mgr = LocalWorkflowDefinitionManager(db)
        rows = mgr.list_all(workflow_type="agent")
        assert rows == []

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
    def test_sync_respects_soft_deletes(self, tmp_path: Path) -> None:
        """Test that sync does not re-create soft-deleted agents."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "test-agent.yaml").write_text(
            "name: test-agent\ndescription: A test agent\nprovider: claude\nmode: interactive\n"
        )

        mgr = LocalWorkflowDefinitionManager(db)

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            # First sync — creates installed row
            sync_bundled_agents(db)

            # Soft-delete the row
            row = mgr.get_by_name("test-agent")
            assert row is not None
            mgr.delete(row.id)

            # Second sync — should skip the soft-deleted row
            result = sync_bundled_agents(db)
            assert result["skipped"] == 1
            assert result["synced"] == 0

    @pytest.mark.unit
    def test_sync_soft_deletes_removed_bundled_agents(self, tmp_path: Path) -> None:
        """Bundled agent rows disappear when their YAML is removed from disk."""
        db = _setup_db(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_yaml = agents_dir / "test-agent.yaml"
        agent_yaml.write_text(
            "name: test-agent\ndescription: A test agent\nprovider: claude\nmode: interactive\n"
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            sync_bundled_agents(db)
            agent_yaml.unlink()
            result = sync_bundled_agents(db)

        assert result["orphaned"] == 1
        assert LocalWorkflowDefinitionManager(db).get_by_name("test-agent") is None

    @pytest.mark.integration
    def test_sync_with_real_bundled_agents(self, tmp_path: Path) -> None:
        """Test that sync works with the actual bundled agents directory."""
        db = _setup_db(tmp_path)

        result = sync_bundled_agents(db)

        assert result["success"] is True
        # At least one bundled agent should be synced (some may be skipped
        # due to name collision with bundled workflows)
        assert result["synced"] + result["skipped"] + result["updated"] >= 1
        assert result["errors"] == []

        # Verify agents are in workflow_definitions
        mgr = LocalWorkflowDefinitionManager(db)
        rows = mgr.list_all(workflow_type="agent")
        assert len(rows) > 0
        names = [r.name for r in rows]
        # Check for agents from the new-format bundled definitions
        assert "default" in names
        assert "backend-developer" in names
        assert "frontend-developer" in names
        assert "qa-reviewer" in names
        assert "doc-reviewer" in names
        assert all(
            n in names
            for n in (
                "analyst",
                "researcher",
                "architect",
                "product-manager",
                "planner",
                "plan-adversary",
            )
        )
        assert "test-architect" not in names
        assert "requirements-analyst" not in names
        assert "conductor" not in names
        assert "developer" not in names
        assert "pipeline-worker" not in names
