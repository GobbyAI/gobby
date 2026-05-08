"""Tests for verification-before-completion lifecycle rule wiring."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> Iterator[LocalDatabase]:
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
    # Test-only bypass: source-change validation is the behavior under test,
    # and the official update API would only add unrelated workflow policy checks.
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")


def _rule(manager: LocalWorkflowDefinitionManager, name: str) -> RuleDefinitionBody:
    row = manager.get_by_name(name)
    assert row is not None
    return RuleDefinitionBody.model_validate_json(row.definition_json)


def test_schema_lookup_rule_mentions_lifecycle_completion_tools(db, manager) -> None:
    _sync_bundled(db)

    body = _rule(manager, "require-verification-before-completion-on-schema")

    assert body.event.value == "before_tool"
    assert body.effects[0].type == "block"
    assert (
        body.effects[0].reason
        == 'Call get_skill(name="verification-before-completion") on gobby-skills, then continue.'
    )
    assert "not skill_loaded('verification-before-completion')" in (body.when or "")
    when = body.when or ""
    for tool_name in (
        "close_task",
        "submit_for_review",
        "approve_review",
        "record_pr_opened",
        "record_pr_verdict",
        "record_merge_result",
        "close_linked_github_issue",
        "merge_apply",
    ):
        assert tool_name in when


def test_lifecycle_call_rule_requires_verification_skill(db, manager) -> None:
    _sync_bundled(db)

    body = _rule(manager, "require-verification-before-completion-on-lifecycle-call")
    block_effects = [effect for effect in body.effects if effect.type == "block"]

    assert body.event.value == "before_tool"
    assert "not skill_loaded('verification-before-completion')" in (body.when or "")
    assert len(block_effects) == 1
    assert (
        block_effects[0].reason
        == 'Call get_skill(name="verification-before-completion") on gobby-skills, then continue.'
    )
