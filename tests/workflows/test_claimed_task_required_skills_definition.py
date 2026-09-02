"""Contract for the claimed-task required-skills rule definition."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

RULE_NAME = "require-claimed-task-required-skills"
RULE_FILE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "gobby"
    / "install"
    / "shared"
    / "workflows"
    / "rules"
    / "task-enforcement"
    / f"{RULE_NAME}.yaml"
)
DISCLOSURE_RULE_NAME = "disclose-claimed-task-required-skills"
DISCLOSURE_RULE_FILE = RULE_FILE.with_name(f"{DISCLOSURE_RULE_NAME}.yaml")


def test_source_rule_parses_to_canonical_installed_definition(temp_db: HubDatabase) -> None:
    source = yaml.safe_load(RULE_FILE.read_text())
    source_rule = source["rules"][RULE_NAME]

    assert set(source["rules"]) == {RULE_NAME}
    assert source_rule["when"] == source_rule["when"].strip()

    result = sync_bundled_rules(temp_db, get_bundled_rules_path())
    assert result["errors"] == []
    row = RuleDefinitionManager(temp_db).get_by_name(RULE_NAME)
    assert row is not None
    installed = RuleDefinitionBody.model_validate(row.definition_json)

    assert installed.when == source_rule["when"]
    assert row.description == source_rule["description"]
    assert installed.effects is not None
    assert installed.effects[0].type == source_rule["effects"][0]["type"]
    assert installed.effects[0].reason == source_rule["effects"][0]["reason"]


def test_claim_disclosure_rule_injects_every_missing_required_skill(
    temp_db: HubDatabase,
) -> None:
    source = yaml.safe_load(DISCLOSURE_RULE_FILE.read_text())
    source_rule = source["rules"][DISCLOSURE_RULE_NAME]

    assert set(source["rules"]) == {DISCLOSURE_RULE_NAME}

    result = sync_bundled_rules(temp_db, get_bundled_rules_path())
    assert result["errors"] == []
    row = RuleDefinitionManager(temp_db).get_by_name(DISCLOSURE_RULE_NAME)
    assert row is not None
    installed = RuleDefinitionBody.model_validate(row.definition_json)

    assert installed.group == "task-enforcement"
    assert installed.event.value == "after_tool"
    assert installed.effects is not None
    assert installed.effects[0].type == "inject_context"
    assert installed.effects[0].template == source_rule["effects"][0]["template"]
    helper = "missing_claimed_task_required_skills(tool_input, event.data)"
    assert f"{helper} != []" in (installed.when or "")
    assert f"skill_fetch_batch_directive({helper})" in (installed.effects[0].template or "")
    assert "$VAR" in (installed.effects[0].template or "")
