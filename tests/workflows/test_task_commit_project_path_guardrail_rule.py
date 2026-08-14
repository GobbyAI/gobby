"""Rule tests for task commit project_path allowlist guardrail."""

from __future__ import annotations

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit


def test_task_commit_project_path_guardrail_rule_syncs_and_validates(
    temp_db: HubDatabase,
) -> None:
    result = sync_bundled_rules(temp_db, get_bundled_rules_path())
    assert result["errors"] == []

    temp_db.execute(
        "UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'"
    )
    row = RuleDefinitionManager(temp_db).get_by_name(
        "task-commit-project-path-allowlist-before-git"
    )

    assert row is not None
    rule_yaml = (
        get_bundled_rules_path()
        / "task-enforcement"
        / "task-commit-project-path-allowlist-before-git.yaml"
    ).read_text(encoding="utf-8")
    assert "priority: 30" in rule_yaml

    body = RuleDefinitionBody.model_validate(row.definition_json)
    assert body.event.value == "before_tool"
    assert body.group == "task-enforcement"
    assert body.when is not None
    assert "canonical_repo_mutation" in body.when
    assert "task_commit_project_path_allowlist_violation" in body.when
    assert [effect.type for effect in body.resolved_effects] == ["block"]
