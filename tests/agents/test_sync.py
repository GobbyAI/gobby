"""Tests for sync_bundled_agents."""

import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path, sync_bundled_agents
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody


def _mgr(db: HubDatabase) -> AgentDefinitionManager:
    return AgentDefinitionManager(db)


def _parse_body(row: object) -> AgentDefinitionBody:
    payload = row.definition_json  # type: ignore[attr-defined]
    if isinstance(payload, str):
        return AgentDefinitionBody.model_validate_json(payload)
    return AgentDefinitionBody.model_validate(payload)


class TestSyncBundledAgents:
    """Tests for sync_bundled_agents function."""

    @pytest.mark.unit
    def test_sync_creates_bundled_agents(
        self, tmp_path: Path, definition_db: PostgresHubDatabase
    ) -> None:
        """Test that sync creates installed agent definitions directly."""
        db = definition_db

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
        mgr = _mgr(db)
        rows = mgr.list_all()
        row = next((r for r in rows if r.name == "test-agent"), None)
        assert row is not None
        assert row.source == "installed"
        body = _parse_body(row)
        assert body.name == "test-agent"

    @pytest.mark.unit
    def test_sync_skips_unchanged(self, tmp_path: Path, definition_db: PostgresHubDatabase) -> None:
        """Test that sync skips agents that already exist."""
        db = definition_db

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
    def test_sync_uses_filename_when_yaml_name_is_null(
        self, tmp_path: Path, definition_db: PostgresHubDatabase
    ) -> None:
        """A null name should not become a managed orphan key."""
        db = definition_db

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "filename-agent.yaml").write_text(
            "name: null\ndescription: From filename\nprovider: claude\nmode: interactive\n"
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["success"] is True
        assert result["synced"] == 1
        row = _mgr(db).get_by_name("filename-agent")
        assert row is not None

    @pytest.mark.unit
    def test_sync_updates_existing_installed_definition(
        self, tmp_path: Path, definition_db: PostgresHubDatabase
    ) -> None:
        """Installed bundled agents should update when the template definition changes."""
        db = definition_db

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
        mgr = _mgr(db)
        rows = mgr.list_all()
        row = next((r for r in rows if r.name == "test-agent"), None)
        assert row is not None
        body = _parse_body(row)
        assert body.description == "Updated description"

    @pytest.mark.unit
    def test_sync_repairs_stale_generated_step_workflow_for_unchanged_agent(
        self,
        tmp_path: Path,
        definition_db: PostgresHubDatabase,
    ) -> None:
        """Agent sync should refresh a stale child workflow even when the parent row skips."""
        db = definition_db

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_yaml = (
            "name: merge-helper\n"
            "description: Merge helper\n"
            "provider: claude\n"
            "mode: interactive\n"
            "step_workflow:\n"
            "  steps:\n"
            "    - name: merge\n"
            "      allowed_tools:\n"
            "        - mcp__gobby__call_tool\n"
            "      allowed_mcp_tools:\n"
            "        - gobby-worktrees:get_worktree\n"
            "        - gobby-merge:inspect_merge_state\n"
        )
        (agents_dir / "merge-helper.yaml").write_text(agent_yaml)

        body = AgentDefinitionBody.model_validate(yaml.safe_load(agent_yaml))
        mgr = _mgr(db)
        created = mgr.create(
            name="merge-helper",
            definition_json=body.model_dump_json(),
            description=body.description,
            source="installed",
            enabled=body.enabled,
            tags=["gobby"],
        )
        mgr.set_step_workflow(
            created.id,
            {
                "steps": [
                    {
                        "name": "merge",
                        "allowed_tools": ["mcp__gobby__call_tool"],
                        "allowed_mcp_tools": ["gobby-worktrees:merge_worktree"],
                    }
                ],
                "variables": {},
                "exit_condition": None,
            },
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["skipped"] == 1
        step_body = mgr.get(created.id).definition_json["step_workflow"]
        allowed = step_body["steps"][0]["allowed_mcp_tools"]
        assert allowed == [
            "gobby-worktrees:get_worktree",
            "gobby-merge:inspect_merge_state",
        ]

    @pytest.mark.unit
    def test_sync_enables_legacy_discovery_placeholder(
        self, tmp_path: Path, definition_db: PostgresHubDatabase
    ) -> None:
        """Old disabled discovery placeholders should become enabled real agents on upgrade."""
        db = definition_db

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
                "model: gpt-5.6-sol\n"
                "reasoning_effort: high\n"
                "instructions: Real ideation agent\n"
            )
            result = sync_bundled_agents(db)

        assert result["updated"] == 1
        mgr = _mgr(db)
        row = mgr.get_by_name("analyst")
        assert row is not None
        assert row.enabled is True
        body = _parse_body(row)
        assert body.enabled is True
        assert body.provider == "codex"

    @pytest.mark.unit
    def test_sync_preserves_user_disabled_non_placeholder_agent(
        self, tmp_path: Path, definition_db: PostgresHubDatabase
    ) -> None:
        """Template updates should not re-enable unrelated user-disabled bundled agents."""
        db = definition_db

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
            mgr = _mgr(db)
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
        row = _mgr(db).get_by_name("test-agent")
        assert row is not None
        assert row.enabled is False

    @pytest.mark.unit
    def test_sync_updates_legacy_template_agent_row(
        self, tmp_path: Path, definition_db: PostgresHubDatabase
    ) -> None:
        """Old gobby template rows should become installed bundled rows."""
        db = definition_db

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "sample-agent.yaml").write_text(
            "name: sample-agent\n"
            "description: Active sample agent\n"
            "enabled: true\n"
            "provider: codex\n"
            "mode: interactive\n"
            "instructions: Build features\n"
        )

        mgr = _mgr(db)
        mgr.create(
            name="sample-agent",
            definition_json=json.dumps(
                {
                    "name": "sample-agent",
                    "description": "Old sample agent",
                    "enabled": False,
                    "provider": "codex",
                    "mode": "interactive",
                    "instructions": "Old implementation",
                }
            ),
            source="installed",
            enabled=False,
            tags=["gobby"],
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["updated"] == 1
        row = mgr.get_by_name("sample-agent")
        assert row is not None
        assert row.source == "installed"
        assert row.enabled is True
        body = _parse_body(row)
        assert body.enabled is True

    @pytest.mark.unit
    def test_sync_restores_reintroduced_bundled_agent(
        self, tmp_path: Path, definition_db: PostgresHubDatabase
    ) -> None:
        """A changed bundled agent can return after a prior bundled orphan delete."""
        db = definition_db

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "sample-agent.yaml").write_text(
            "name: sample-agent\n"
            "description: Active sample agent\n"
            "enabled: true\n"
            "provider: codex\n"
            "mode: interactive\n"
            "instructions: Build features\n"
        )

        mgr = _mgr(db)
        row = mgr.create(
            name="sample-agent",
            definition_json=json.dumps(
                {
                    "name": "sample-agent",
                    "description": "Old sample agent",
                    "enabled": False,
                    "provider": "codex",
                    "mode": "interactive",
                    "instructions": "Old implementation",
                }
            ),
            source="installed",
            enabled=False,
            tags=["gobby"],
        )
        mgr.delete(row.id)

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["updated"] == 1
        restored = mgr.get_by_name("sample-agent")
        assert restored is not None
        assert restored.enabled is True
        body = _parse_body(restored)
        assert body.description == "Active sample agent"

    @pytest.mark.unit
    def test_sync_multiple_agents(self, tmp_path: Path, definition_db: PostgresHubDatabase) -> None:
        """Test syncing multiple agent files."""
        db = definition_db

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "agent-a.yaml").write_text(
            "name: agent-a\nprovider: claude\nmode: interactive\n"
        )
        (agents_dir / "agent-b.yaml").write_text(
            "name: agent-b\nprovider: qwen\nmode: interactive\n"
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["synced"] == 2
        assert result["errors"] == []

    @pytest.mark.unit
    def test_sync_missing_path(self, tmp_path: Path, definition_db: PostgresHubDatabase) -> None:
        """Test sync handles missing agents directory gracefully."""
        db = definition_db

        with patch(
            "gobby.agents.sync.get_bundled_agents_path",
            return_value=tmp_path / "nonexistent",
        ):
            result = sync_bundled_agents(db)

        assert result["success"] is True
        assert result["synced"] == 0
        assert len(result["errors"]) == 1

    @pytest.mark.unit
    def test_sync_ignores_deprecated_directory(
        self, tmp_path: Path, definition_db: PostgresHubDatabase
    ) -> None:
        """Deprecated bundled agents are archival and not active install inputs."""
        db = definition_db

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

        mgr = _mgr(db)
        rows = mgr.list_all()
        assert rows == []

    @pytest.mark.unit
    def test_sync_invalid_yaml(self, tmp_path: Path, definition_db: PostgresHubDatabase) -> None:
        """Test sync handles invalid YAML gracefully."""
        db = definition_db

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "bad.yaml").write_text("not: valid: yaml: [[[")

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["synced"] == 0
        assert len(result["errors"]) == 1

    @pytest.mark.unit
    def test_sync_respects_soft_deletes(
        self, tmp_path: Path, definition_db: PostgresHubDatabase
    ) -> None:
        """Test that sync does not re-create soft-deleted agents."""
        db = definition_db

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "test-agent.yaml").write_text(
            "name: test-agent\ndescription: A test agent\nprovider: claude\nmode: interactive\n"
        )

        mgr = _mgr(db)

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
    def test_sync_reports_unmanaged_shadow_row_loudly(
        self, tmp_path: Path, definition_db: PostgresHubDatabase
    ) -> None:
        """A user-owned row occupying a bundled agent name is a loud sync error."""
        db = definition_db

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "test-agent.yaml").write_text(
            "name: test-agent\ndescription: Bundled template\nprovider: claude\nmode: interactive\n"
        )

        mgr = _mgr(db)
        user_row = mgr.create(
            name="test-agent",
            definition_json=json.dumps(
                {"name": "test-agent", "provider": "claude", "mode": "interactive"}
            ),
            source="installed",
            tags=["user"],
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["shadowed"] == 1
        assert result["synced"] == 0
        assert result["updated"] == 0
        assert any("shadowed" in error for error in result["errors"])

        # The user row is preserved untouched — sync must not clobber it.
        row = mgr.get(user_row.id)
        assert row is not None
        assert row.tags == ["user"]
        body = _parse_body(row)
        assert body.description is None

    @pytest.mark.unit
    def test_sync_reports_soft_deleted_unmanaged_shadow_row_loudly(
        self, tmp_path: Path, definition_db: PostgresHubDatabase
    ) -> None:
        """A soft-deleted user row still shadows the bundled name and errors loudly."""
        db = definition_db

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "test-agent.yaml").write_text(
            "name: test-agent\ndescription: Bundled template\nprovider: claude\nmode: interactive\n"
        )

        mgr = _mgr(db)
        user_row = mgr.create(
            name="test-agent",
            definition_json=json.dumps(
                {"name": "test-agent", "provider": "claude", "mode": "interactive"}
            ),
            source="installed",
            tags=["user"],
        )
        mgr.delete(user_row.id)

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["shadowed"] == 1
        assert any("soft-deleted" in error for error in result["errors"])

    @pytest.mark.unit
    def test_sync_soft_deletes_removed_bundled_agents(
        self, tmp_path: Path, definition_db: PostgresHubDatabase
    ) -> None:
        """Bundled agent rows disappear when their YAML is removed from disk."""
        db = definition_db

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
        assert _mgr(db).get_by_name("test-agent") is None

    @pytest.mark.unit
    def test_sync_soft_deletes_removed_plan_review_researcher(
        self, definition_db: PostgresHubDatabase
    ) -> None:
        """The deleted bundled researcher slug is retired from installed state."""
        mgr = _mgr(definition_db)
        removed = mgr.create(
            name="plan-review-researcher-taskless",
            definition_json=json.dumps(
                {
                    "name": "plan-review-researcher-taskless",
                    "provider": "inherit",
                    "mode": "interactive",
                }
            ),
            source="installed",
            enabled=True,
            tags=["gobby"],
        )

        result = sync_bundled_agents(definition_db)

        assert result["orphaned"] == 1
        deleted = mgr.get(removed.id, include_deleted=True)
        assert deleted is not None
        assert deleted.deleted_at is not None
        assert mgr.get_by_name("plan-review-researcher-taskless") is None

    @pytest.mark.unit
    def test_sync_orphan_cleanup_preserves_non_sync_managed_agents(
        self,
        tmp_path: Path,
        definition_db: PostgresHubDatabase,
    ) -> None:
        """Missing-on-disk agent rows survive unless bundled sync owns them."""
        db = definition_db
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        mgr = _mgr(db)
        definition_json = json.dumps(
            {
                "name": "orphan-agent",
                "provider": "codex",
                "mode": "interactive",
            }
        )
        project_id = str(uuid4())

        owned_orphan = mgr.create(
            name="owned-orphan",
            definition_json=definition_json,
            source="installed",
            tags=["gobby"],
        )
        project_orphan = mgr.create(
            name="project-orphan",
            definition_json=definition_json,
            project_id=project_id,
            source="installed",
            tags=["gobby"],
        )
        custom_orphan = mgr.create(
            name="custom-orphan",
            definition_json=definition_json,
            source="custom",
            tags=["gobby"],
        )

        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            result = sync_bundled_agents(db)

        assert result["orphaned"] == 1
        assert mgr.get(owned_orphan.id, include_deleted=True).deleted_at is not None
        assert mgr.get(project_orphan.id).deleted_at is None
        assert mgr.get(custom_orphan.id).deleted_at is None

    @pytest.mark.integration
    def test_sync_with_real_bundled_agents(
        self, tmp_path: Path, definition_db: PostgresHubDatabase
    ) -> None:
        """Test that sync works with the actual bundled agents directory."""
        db = definition_db

        result = sync_bundled_agents(db)

        assert result["success"] is True
        # At least one bundled agent should be synced (some may be skipped
        # due to name collision with bundled workflows)
        assert result["synced"] + result["skipped"] + result["updated"] >= 1
        assert result["errors"] == []

        # Verify agents are in rule_definitions
        mgr = _mgr(db)
        rows = mgr.list_all()
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

        children = {
            row["name"]: row["child"]
            for row in db.fetchall(
                """
                SELECT a.name, w.id IS NOT NULL AS child
                FROM agent_definitions a
                LEFT JOIN agent_step_workflows w ON w.agent_definition_id = a.id
                WHERE a.deleted_at IS NULL
                """
            )
        }
        stepful = [name for name, has_child in children.items() if has_child]
        stepless = [name for name, has_child in children.items() if not has_child]
        assert len(stepful) == 21
        assert set(stepless) == _STEPLESS_BUNDLED_AGENTS
        for row in db.fetchall("SELECT name, definition_json FROM agent_definitions"):
            body = row["definition_json"]
            if isinstance(body, str):
                body = json.loads(body)
            assert isinstance(body, dict)
            assert "steps" not in body
            assert "step_variables" not in body
            assert "exit_condition" not in body
            assert "step_workflow" not in body

    @pytest.mark.unit
    def test_sync_adopts_enabled_default_unless_pinned(
        self, tmp_path: Path, definition_db: PostgresHubDatabase
    ) -> None:
        db = definition_db
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        yaml_file = agents_dir / "toggle-agent.yaml"
        yaml_file.write_text(
            "name: toggle-agent\nenabled: false\nprovider: claude\nmode: interactive\n"
        )
        with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
            sync_bundled_agents(db)
            mgr = _mgr(db)
            untouched = mgr.get_by_name("toggle-agent")
            assert untouched is not None
            assert untouched.enabled is False
            pinned = mgr.create(
                name="pinned-agent",
                definition_json={"name": "pinned-agent", "provider": "claude"},
                source="installed",
                enabled=False,
                tags=["gobby"],
            )
            mgr.update(pinned.id, enabled=False)
            (agents_dir / "pinned-agent.yaml").write_text(
                "name: pinned-agent\nenabled: true\nprovider: claude\nmode: interactive\n"
            )
            yaml_file.write_text(
                "name: toggle-agent\nenabled: true\nprovider: claude\nmode: interactive\n"
            )
            result = sync_bundled_agents(db)

        assert result["updated"] >= 1
        toggle = mgr.get_by_name("toggle-agent")
        assert toggle is not None
        assert toggle.enabled is True
        pinned_row = mgr.get(pinned.id)
        assert pinned_row.enabled is False
        assert pinned_row.enabled_pinned is True


_STEPLESS_BUNDLED_AGENTS = frozenset({"comms-agent", "default", "goal-taskmaster", "triage-agent"})
_LEGACY_STEP_KEYS = ("steps", "step_variables", "exit_condition")


@pytest.mark.unit
def test_bundled_agents_nested_step_workflow() -> None:
    """All 25 bundled agents load under the nested step_workflow model."""
    from gobby.workflows.definitions import AgentDefinitionBody

    paths = sorted(get_bundled_agents_path().glob("*.yaml"))
    assert len(paths) == 25

    for path in paths:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        for key in _LEGACY_STEP_KEYS:
            assert key not in raw, f"{path.name} still has top-level {key}"
        body = AgentDefinitionBody.model_validate(raw)
        if path.stem in _STEPLESS_BUNDLED_AGENTS:
            assert body.step_workflow is None, path.name
        else:
            assert body.step_workflow is not None, path.name
            assert len(body.step_workflow.steps) >= 1, path.name


@pytest.mark.unit
def test_memory_recall_helper_not_bundled(definition_db: PostgresHubDatabase) -> None:
    """Memory recall is daemon-owned and no helper agent is bundled."""
    result = sync_bundled_agents(definition_db)

    assert result["success"] is True
    assert result["errors"] == []

    row = _mgr(definition_db).get_by_name("memory-recall-helper")
    assert row is None
