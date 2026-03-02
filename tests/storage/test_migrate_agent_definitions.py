"""Tests for v117 migration: agent_definitions → workflow_definitions.

Covers:
- Data migration: 29-field rows → 12-field AgentDefinitionBody in workflow_definitions
- Field mapping: role/goal/personality/instructions → composed instructions
- Table drop: agent_definitions removed after migration
- Consumer compatibility: sync and loader work with workflow_definitions
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit

# Old agent_definitions schema (pre-v117) for test setup
_AGENT_DEFINITIONS_DDL = """
CREATE TABLE IF NOT EXISTS agent_definitions (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    name TEXT NOT NULL,
    description TEXT,
    role TEXT,
    goal TEXT,
    personality TEXT,
    instructions TEXT,
    provider TEXT NOT NULL DEFAULT 'claude',
    model TEXT,
    mode TEXT NOT NULL DEFAULT 'headless',
    terminal TEXT DEFAULT 'auto',
    isolation TEXT,
    base_branch TEXT DEFAULT 'main',
    timeout REAL DEFAULT 120.0,
    max_turns INTEGER DEFAULT 10,
    default_workflow TEXT,
    sandbox_config TEXT,
    skill_profile TEXT,
    workflows TEXT,
    lifecycle_variables TEXT,
    default_variables TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    scope TEXT NOT NULL DEFAULT 'global',
    source_path TEXT,
    version TEXT DEFAULT '1.0',
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_defs_name_scope_project
    ON agent_definitions(name, scope, COALESCE(project_id, ''));
"""


@pytest.fixture
def db_with_old_table(tmp_path) -> LocalDatabase:
    """Database with baseline schema + old agent_definitions table."""
    db = LocalDatabase(tmp_path / "test_v117.db")
    run_migrations(db)
    # Create old table manually (baseline no longer includes it after v117)
    for stmt in _AGENT_DEFINITIONS_DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            db.execute(stmt)
    return db


def _insert_old_agent(
    db: LocalDatabase,
    name: str,
    **kwargs: object,
) -> str:
    """Insert a row into the old agent_definitions table."""
    row_id = str(uuid4())
    defaults: dict[str, object] = {
        "id": row_id,
        "project_id": None,
        "name": name,
        "description": None,
        "role": None,
        "goal": None,
        "personality": None,
        "instructions": None,
        "provider": "claude",
        "model": None,
        "mode": "autonomous",
        "terminal": "auto",
        "isolation": None,
        "base_branch": "main",
        "timeout": 120.0,
        "max_turns": 10,
        "default_workflow": None,
        "sandbox_config": None,
        "skill_profile": None,
        "workflows": None,
        "lifecycle_variables": None,
        "default_variables": None,
        "enabled": 1,
        "scope": "global",
        "source_path": None,
        "version": "1.0",
        "deleted_at": None,
    }
    defaults.update(kwargs)

    cols = list(defaults.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_names = ", ".join(cols)
    values = [defaults[c] for c in cols]

    db.execute(
        f"INSERT INTO agent_definitions ({col_names}) VALUES ({placeholders})",
        tuple(values),
    )
    return row_id


# ═══════════════════════════════════════════════════════════════════════
# Migration function tests
# ═══════════════════════════════════════════════════════════════════════


class TestMigrateAgentDefinitions:
    """v117: agent_definitions rows → workflow_definitions with workflow_type='agent'."""

    def test_migrates_basic_agent(self, db_with_old_table: LocalDatabase) -> None:
        from gobby.storage.migrations import _migrate_agent_defs_to_workflow_defs

        db = db_with_old_table
        _insert_old_agent(db, "test-basic-mig", provider="claude", mode="autonomous")

        _migrate_agent_defs_to_workflow_defs(db)

        row = db.fetchone(
            "SELECT * FROM workflow_definitions WHERE name = ? AND workflow_type = 'agent'",
            ("test-basic-mig",),
        )
        assert row is not None
        body = AgentDefinitionBody.model_validate_json(row["definition_json"])
        assert body.name == "test-basic-mig"
        assert body.provider == "claude"

    def test_maps_all_preserved_fields(self, db_with_old_table: LocalDatabase) -> None:
        from gobby.storage.migrations import _migrate_agent_defs_to_workflow_defs

        db = db_with_old_table
        _insert_old_agent(
            db,
            "test-full-mig",
            description="Full agent",
            provider="gemini",
            model="gemini-2.5-pro",
            mode="terminal",
            isolation="worktree",
            base_branch="develop",
            timeout=300.0,
            max_turns=25,
        )

        _migrate_agent_defs_to_workflow_defs(db)

        row = db.fetchone(
            "SELECT * FROM workflow_definitions WHERE name = ? AND workflow_type = 'agent'",
            ("test-full-mig",),
        )
        assert row is not None
        body = AgentDefinitionBody.model_validate_json(row["definition_json"])
        assert body.provider == "gemini"
        assert body.model == "gemini-2.5-pro"
        assert body.mode == "terminal"
        assert body.isolation == "worktree"
        assert body.base_branch == "develop"
        assert body.timeout == 300.0
        assert body.max_turns == 25
        assert row["description"] == "Full agent"

    def test_composes_instructions_from_structured_fields(
        self, db_with_old_table: LocalDatabase
    ) -> None:
        from gobby.storage.migrations import _migrate_agent_defs_to_workflow_defs

        db = db_with_old_table
        _insert_old_agent(
            db,
            "test-composed-mig",
            role="Senior developer",
            goal="Ship clean code",
            personality="Concise and direct",
            instructions="Follow TDD.",
        )

        _migrate_agent_defs_to_workflow_defs(db)

        row = db.fetchone(
            "SELECT * FROM workflow_definitions WHERE name = ? AND workflow_type = 'agent'",
            ("test-composed-mig",),
        )
        body = AgentDefinitionBody.model_validate_json(row["definition_json"])
        assert "Senior developer" in body.instructions
        assert "Ship clean code" in body.instructions
        assert "Concise and direct" in body.instructions
        assert "Follow TDD." in body.instructions
        # Structured sections should be present
        assert "## Role" in body.instructions
        assert "## Goal" in body.instructions

    def test_instructions_only_when_no_structured_fields(
        self, db_with_old_table: LocalDatabase
    ) -> None:
        from gobby.storage.migrations import _migrate_agent_defs_to_workflow_defs

        db = db_with_old_table
        _insert_old_agent(
            db,
            "test-instronly-mig",
            instructions="Just do it.",
        )

        _migrate_agent_defs_to_workflow_defs(db)

        row = db.fetchone(
            "SELECT * FROM workflow_definitions WHERE name = ? AND workflow_type = 'agent'",
            ("test-instronly-mig",),
        )
        body = AgentDefinitionBody.model_validate_json(row["definition_json"])
        assert body.instructions == "Just do it."

    def test_preserves_enabled_flag(self, db_with_old_table: LocalDatabase) -> None:
        from gobby.storage.migrations import _migrate_agent_defs_to_workflow_defs

        db = db_with_old_table
        _insert_old_agent(db, "test-disabled-mig", enabled=0)

        _migrate_agent_defs_to_workflow_defs(db)

        row = db.fetchone(
            "SELECT * FROM workflow_definitions WHERE name = ? AND workflow_type = 'agent'",
            ("test-disabled-mig",),
        )
        assert row is not None
        assert row["enabled"] == 0
        body = AgentDefinitionBody.model_validate_json(row["definition_json"])
        assert body.enabled is False

    def test_drops_agent_definitions_table(self, db_with_old_table: LocalDatabase) -> None:
        from gobby.storage.migrations import _migrate_agent_defs_to_workflow_defs

        db = db_with_old_table
        _insert_old_agent(db, "test-drop-mig")

        _migrate_agent_defs_to_workflow_defs(db)

        # Table should not exist
        result = db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_definitions'"
        )
        assert result is None

    def test_skips_soft_deleted_rows(self, db_with_old_table: LocalDatabase) -> None:
        from gobby.storage.migrations import _migrate_agent_defs_to_workflow_defs

        db = db_with_old_table
        _insert_old_agent(db, "test-deleted-mig", deleted_at="2025-01-01T00:00:00+00:00")
        _insert_old_agent(db, "test-active-mig")

        _migrate_agent_defs_to_workflow_defs(db)

        deleted = db.fetchone(
            "SELECT * FROM workflow_definitions WHERE name = ? AND workflow_type = 'agent'",
            ("test-deleted-mig",),
        )
        active = db.fetchone(
            "SELECT * FROM workflow_definitions WHERE name = ? AND workflow_type = 'agent'",
            ("test-active-mig",),
        )
        assert deleted is None
        assert active is not None

    def test_handles_null_optional_fields(self, db_with_old_table: LocalDatabase) -> None:
        from gobby.storage.migrations import _migrate_agent_defs_to_workflow_defs

        db = db_with_old_table
        # Minimal row with all optional fields null
        _insert_old_agent(db, "test-minimal-mig")

        _migrate_agent_defs_to_workflow_defs(db)

        row = db.fetchone(
            "SELECT * FROM workflow_definitions WHERE name = ? AND workflow_type = 'agent'",
            ("test-minimal-mig",),
        )
        assert row is not None
        body = AgentDefinitionBody.model_validate_json(row["definition_json"])
        assert body.name == "test-minimal-mig"
        assert body.instructions is None
        assert body.model is None

    def test_maps_scope_to_source(self, db_with_old_table: LocalDatabase) -> None:
        from gobby.storage.migrations import _migrate_agent_defs_to_workflow_defs

        db = db_with_old_table
        _insert_old_agent(db, "test-bundled-mig", scope="bundled")
        _insert_old_agent(db, "test-global-mig", scope="global")

        _migrate_agent_defs_to_workflow_defs(db)

        bundled = db.fetchone(
            "SELECT source FROM workflow_definitions WHERE name = ? AND workflow_type = 'agent'",
            ("test-bundled-mig",),
        )
        global_ = db.fetchone(
            "SELECT source FROM workflow_definitions WHERE name = ? AND workflow_type = 'agent'",
            ("test-global-mig",),
        )
        assert bundled["source"] == "template"
        assert global_["source"] == "installed"

    def test_migrates_multiple_agents(self, db_with_old_table: LocalDatabase) -> None:
        from gobby.storage.migrations import _migrate_agent_defs_to_workflow_defs

        db = db_with_old_table
        for i in range(5):
            _insert_old_agent(db, f"test-multi-mig-{i}")

        _migrate_agent_defs_to_workflow_defs(db)

        rows = db.fetchall(
            "SELECT * FROM workflow_definitions WHERE workflow_type = 'agent' AND name LIKE 'test-multi-mig-%'"
        )
        assert len(rows) == 5


# ═══════════════════════════════════════════════════════════════════════
# Consumer compatibility tests
# ═══════════════════════════════════════════════════════════════════════


class TestSyncBundledAgentsAfterMigration:
    """sync_bundled_agents works with workflow_definitions storage."""

    def test_syncs_agents_to_workflow_definitions(self, tmp_path) -> None:
        from gobby.agents.sync import sync_bundled_agents

        db = LocalDatabase(tmp_path / "test_sync_after.db")
        run_migrations(db)

        result = sync_bundled_agents(db)
        assert result["success"] is True
        assert result["synced"] + result["updated"] + result["skipped"] > 0

        # Verify agents are in workflow_definitions
        rows = db.fetchall(
            "SELECT * FROM workflow_definitions WHERE workflow_type = 'agent'"
        )
        assert len(rows) > 0
        # Verify at least one known bundled agent was synced
        names = [r["name"] for r in rows]
        assert any(n in names for n in ("default", "claude-cli-sonnet", "gemini-cli"))

    def test_sync_is_idempotent(self, tmp_path) -> None:
        from gobby.agents.sync import sync_bundled_agents

        db = LocalDatabase(tmp_path / "test_sync_idem.db")
        run_migrations(db)

        result1 = sync_bundled_agents(db)
        result2 = sync_bundled_agents(db)

        # Second run should skip all already-synced agents
        assert result2["synced"] == 0
        assert result2["skipped"] >= result1["synced"]


class TestLifecyclePurgeAfterMigration:
    """Lifecycle purge works without agent_definitions table."""

    def test_purge_only_uses_workflow_definitions(self, tmp_path) -> None:
        """After migration, purge should not reference agent_definitions."""
        from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

        db = LocalDatabase(tmp_path / "test_purge_after.db")
        run_migrations(db)

        # Purge should work without agent_definitions table
        wf_mgr = LocalWorkflowDefinitionManager(db)
        purged = wf_mgr.purge_deleted(older_than_days=30)
        assert purged >= 0  # No error is success
