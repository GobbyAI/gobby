"""Tests for verification-before-completion lifecycle rule wiring."""

from __future__ import annotations

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path):
    database = LocalDatabase(tmp_path / "verification_rules.db")
    run_migrations(database)
    yield database
    database.close()


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _sync_bundled(db: LocalDatabase) -> None:
    from gobby.workflows.sync_rules import get_bundled_rules_path

    sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")


def _rule(manager: LocalWorkflowDefinitionManager, name: str) -> RuleDefinitionBody:
    row = manager.get_by_name(name)
    assert row is not None
    return RuleDefinitionBody.model_validate_json(row.definition_json)


def test_schema_lookup_rule_mentions_lifecycle_completion_tools(db, manager) -> None:
    _sync_bundled(db)

    body = _rule(manager, "inject-verification-before-completion-on-schema")

    assert body.event.value == "after_tool"
    when = body.when or ""
    for tool_name in (
        "close_task",
        "submit_for_review",
        "approve_review",
        "record_pr_opened",
        "record_pr_verdict",
        "record_merge_result",
        "merge_apply",
    ):
        assert tool_name in when


def test_lifecycle_call_rule_injects_verification_skill(db, manager) -> None:
    _sync_bundled(db)

    body = _rule(manager, "inject-verification-before-completion-on-lifecycle-call")
    inject_effects = [effect for effect in body.effects if effect.type == "inject_context"]

    assert body.event.value == "before_tool"
    assert len(inject_effects) == 1
    assert 'get_skill(name="verification-before-completion")' in (inject_effects[0].template or "")
    assert "Fresh verification evidence is required" in (inject_effects[0].template or "")
