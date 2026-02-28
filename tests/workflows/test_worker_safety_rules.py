"""Tests for worker-safety rules in new RuleDefinitionBody format.

Verifies the migrated worker-safety.yaml produces identical blocking
behavior to the old rule_definitions format.
"""

from __future__ import annotations

import json

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.sync import sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_worker_safety.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _sync_bundled(db):
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    # Mark templates as installed so get_by_name() finds them without include_templates
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")
    return result


class TestWorkerSafetySync:
    """Test that the bundled worker-safety.yaml syncs correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All worker-safety rules should sync to workflow_definitions."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}

        expected = {"no-push", "no-force-push", "no-destructive-git"}
        assert expected.issubset(rule_names), f"Missing: {expected - rule_names}"

    def test_all_rules_have_group(self, db, manager) -> None:
        """All worker-safety rules should have group='worker-safety'."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            body = json.loads(row.definition_json)
            if row.name in {"no-push", "no-force-push", "no-destructive-git"}:
                assert body.get("group") == "worker-safety", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in {"no-push", "no-force-push", "no-destructive-git"}:
                body = RuleDefinitionBody.model_validate_json(row.definition_json)
                assert body.event.value == "before_tool"
                assert body.effect.type == "block"


class TestNoPushRule:
    """Verify no-push rule blocks git push commands."""

    def test_blocks_bash_with_git_push(self, db, manager) -> None:
        """no-push should block Bash tool with git push."""
        _sync_bundled(db)

        row = manager.get_by_name("no-push")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.effect.tools == ["Bash"]
        assert body.effect.command_pattern is not None
        assert "push" in body.effect.command_pattern


class TestNoForcePushRule:
    """Verify no-force-push rule blocks force push commands."""

    def test_blocks_force_push_flags(self, db, manager) -> None:
        """no-force-push should block force push flags."""
        _sync_bundled(db)

        row = manager.get_by_name("no-force-push")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.effect.tools == ["Bash"]
        assert body.effect.command_pattern is not None
        assert "--force" in body.effect.command_pattern


class TestNoDestructiveGitRule:
    """Verify no-destructive-git rule blocks dangerous git commands."""

    def test_blocks_destructive_commands(self, db, manager) -> None:
        """no-destructive-git should block reset --hard, clean -f, etc."""
        _sync_bundled(db)

        row = manager.get_by_name("no-destructive-git")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.effect.tools == ["Bash"]
        assert body.effect.command_pattern is not None
        assert "reset" in body.effect.command_pattern
