"""Tests for workflows/selectors.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.selectors import (
    _match_rule,
    _match_skill,
    parse_selector,
    rule_matches_agent,
    resolve_rules_for_agent,
    resolve_skills_for_agent,
    resolve_variables_for_agent,
)

pytestmark = pytest.mark.unit


# --- parse_selector ---


def test_parse_selector_with_tag_prefix() -> None:
    assert parse_selector("tag:infra") == ("tag", "infra")


def test_parse_selector_with_name_prefix() -> None:
    assert parse_selector("name:my-rule") == ("name", "my-rule")


def test_parse_selector_with_source_prefix() -> None:
    assert parse_selector("source:installed") == ("source", "installed")


def test_parse_selector_with_group_prefix() -> None:
    assert parse_selector("group:core") == ("group", "core")


def test_parse_selector_with_category_prefix() -> None:
    assert parse_selector("category:dev") == ("category", "dev")


def test_parse_selector_bare_string() -> None:
    assert parse_selector("my-rule") == ("name", "my-rule")


def test_parse_selector_unknown_prefix() -> None:
    assert parse_selector("unknown:val") == ("name", "unknown:val")


# --- _match_rule ---


def test_match_rule_wildcard() -> None:
    rule = MagicMock()
    assert _match_rule("*", "", rule, {}) is True


def test_match_rule_name() -> None:
    rule = MagicMock()
    rule.name = "my-rule"
    assert _match_rule("name", "my-rule", rule, {}) is True
    assert _match_rule("name", "other", rule, {}) is False


def test_match_rule_name_glob() -> None:
    rule = MagicMock()
    rule.name = "require-task-close"
    assert _match_rule("name", "require-*", rule, {}) is True


def test_match_rule_source() -> None:
    rule = MagicMock()
    rule.source = "installed"
    assert _match_rule("source", "installed", rule, {}) is True
    assert _match_rule("source", "template", rule, {}) is False


def test_match_rule_tag() -> None:
    rule = MagicMock()
    rule.tags = ["infra", "core"]
    assert _match_rule("tag", "infra", rule, {}) is True
    assert _match_rule("tag", "missing", rule, {}) is False


def test_match_rule_tag_none() -> None:
    rule = MagicMock()
    rule.tags = None
    assert _match_rule("tag", "any", rule, {}) is False


def test_match_rule_group() -> None:
    rule = MagicMock()
    assert _match_rule("group", "core", rule, {"group": "core"}) is True
    assert _match_rule("group", "core", rule, {"group": "other"}) is False
    assert _match_rule("group", "core", rule, {}) is False


def test_match_rule_unknown_dim() -> None:
    rule = MagicMock()
    assert _match_rule("bogus", "val", rule, {}) is False


# --- resolve_rules_for_agent ---


def test_resolve_rules_explicit_only() -> None:
    agent = MagicMock()
    agent.workflows.rules = ["rule-a", "rule-b"]
    agent.workflows.rule_selectors = None

    result = resolve_rules_for_agent(agent, [])
    assert result == {"rule-a", "rule-b"}


def test_resolve_rules_with_include_selectors() -> None:
    agent = MagicMock()
    agent.workflows.rules = []
    agent.workflows.rule_selectors = MagicMock()
    agent.workflows.rule_selectors.include = ["tag:infra"]
    agent.workflows.rule_selectors.exclude = []

    rule = MagicMock()
    rule.name = "infra-rule"
    rule.tags = ["infra"]
    rule.definition_json = None

    result = resolve_rules_for_agent(agent, [rule])
    assert "infra-rule" in result


def test_resolve_rules_with_exclude() -> None:
    agent = MagicMock()
    agent.workflows.rules = ["rule-a"]
    agent.workflows.rule_selectors = MagicMock()
    agent.workflows.rule_selectors.include = ["name:*"]
    agent.workflows.rule_selectors.exclude = ["name:rule-a"]

    rule = MagicMock()
    rule.name = "rule-a"
    rule.tags = []
    rule.definition_json = None

    result = resolve_rules_for_agent(agent, [rule])
    # rule-a is in explicit AND exclude → exclude wins on include matches but not explicit
    # Actually: combined = explicit | include_matches, then combined - exclude_matches
    # include_matches has rule-a (name:*), exclude has rule-a → removed from combined
    assert "rule-a" not in result


def test_resolve_rules_json_parse_error() -> None:
    agent = MagicMock()
    agent.workflows.rules = []
    agent.workflows.rule_selectors = MagicMock()
    agent.workflows.rule_selectors.include = ["group:core"]
    agent.workflows.rule_selectors.exclude = []

    rule = MagicMock()
    rule.name = "bad-json"
    rule.tags = []
    rule.definition_json = "not valid json{{"

    result = resolve_rules_for_agent(agent, [rule])
    assert "bad-json" not in result  # Can't match group without valid JSON


# --- _match_skill ---


def test_match_skill_wildcard() -> None:
    assert _match_skill("*", "", MagicMock()) is True


def test_match_skill_name() -> None:
    skill = MagicMock()
    skill.name = "commit"
    assert _match_skill("name", "commit", skill) is True
    assert _match_skill("name", "other", skill) is False


def test_match_skill_source() -> None:
    skill = MagicMock()
    skill.source_type = "installed"
    assert _match_skill("source", "installed", skill) is True


def test_match_skill_source_mismatch() -> None:
    skill = MagicMock()
    skill.source_type = "agent"
    assert _match_skill("source", "installed", skill) is False


def test_match_skill_category() -> None:
    skill = MagicMock()
    skill.metadata = {"skillport": {"category": "dev"}}
    assert _match_skill("category", "dev", skill) is True


def test_match_skill_category_gobby() -> None:
    skill = MagicMock()
    skill.metadata = {"gobby": {"category": "ops"}}
    assert _match_skill("category", "ops", skill) is True


def test_match_skill_category_none() -> None:
    skill = MagicMock()
    skill.metadata = None
    assert _match_skill("category", "any", skill) is False


def test_match_skill_tag() -> None:
    skill = MagicMock()
    skill.metadata = {"gobby": {"tags": ["git", "vcs"]}, "skillport": {"tags": ["scm"]}}
    assert _match_skill("tag", "git", skill) is True
    assert _match_skill("tag", "scm", skill) is True
    assert _match_skill("tag", "missing", skill) is False


def test_match_skill_tag_no_metadata() -> None:
    skill = MagicMock()
    skill.metadata = None
    assert _match_skill("tag", "any", skill) is False


def test_match_skill_unknown_dim() -> None:
    assert _match_skill("bogus", "val", MagicMock()) is False


# --- resolve_skills_for_agent ---


def test_resolve_skills_no_selectors() -> None:
    agent = MagicMock()
    agent.workflows.skill_selectors = None
    assert resolve_skills_for_agent(agent, []) is None


def test_resolve_skills_with_include_exclude() -> None:
    agent = MagicMock()
    agent.workflows.skill_selectors = MagicMock()
    agent.workflows.skill_selectors.include = ["name:*"]
    agent.workflows.skill_selectors.exclude = ["name:dangerous"]

    s1 = MagicMock()
    s1.name = "safe"
    s1.metadata = None
    s1.source_type = "agent"

    s2 = MagicMock()
    s2.name = "dangerous"
    s2.metadata = None
    s2.source_type = "agent"

    result = resolve_skills_for_agent(agent, [s1, s2])
    assert result is not None
    assert "safe" in result
    assert "dangerous" not in result


def test_resolve_skills_explicit_only() -> None:
    """When skill_selectors is None, resolve returns None (permissive by default)."""
    agent = MagicMock()
    agent.workflows.skills = ["skill-a", "skill-b"]
    agent.workflows.skill_selectors = None
    result = resolve_skills_for_agent(agent, [])
    assert result is None


def test_bundled_default_agent_auto_selects_no_skills() -> None:
    """Default agent has an empty skill_selectors.include — skills inject
    on-demand via rules (brevity on first turn, code-index on first code read)
    rather than being auto-attached to the agent.
    """
    agent_path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/default.yaml"
    )
    agent = AgentDefinitionBody.model_validate(yaml.safe_load(agent_path.read_text()))

    brevity = MagicMock()
    brevity.name = "brevity"
    brevity.metadata = {"gobby": {"category": "optimization"}}
    brevity.source_type = "installed"

    code_index = MagicMock()
    code_index.name = "code-index"
    code_index.metadata = {"gobby": {"category": "core"}}
    code_index.source_type = "installed"

    result = resolve_skills_for_agent(agent, [brevity, code_index])

    assert result == set()


# --- resolve_variables_for_agent ---


def test_resolve_variables_no_selectors() -> None:
    agent = MagicMock()
    agent.workflows.variable_selectors = None
    assert resolve_variables_for_agent(agent, []) is None


def test_resolve_variables_with_include() -> None:
    agent = MagicMock()
    agent.workflows.variable_selectors = MagicMock()
    agent.workflows.variable_selectors.include = ["name:session-*"]
    agent.workflows.variable_selectors.exclude = []

    var = MagicMock()
    var.name = "session-defaults"
    var.tags = []
    var.definition_json = None

    result = resolve_variables_for_agent(agent, [var])
    assert result is not None
    assert "session-defaults" in result


def test_resolve_rules_tag_exclude_sync() -> None:
    """Agents with exclude: [tag:sync] should exclude sync-tagged rules."""
    agent = MagicMock()
    agent.workflows.rules = []
    agent.workflows.rule_selectors = MagicMock()
    agent.workflows.rule_selectors.include = ["tag:default", "tag:worker-safety"]
    agent.workflows.rule_selectors.exclude = ["tag:sync"]

    # A default-tagged rule (should be included)
    default_rule = MagicMock()
    default_rule.name = "require-task-before-edit"
    default_rule.tags = ["default", "gobby"]
    default_rule.definition_json = None

    # An excluded-tagged rule (should be excluded despite also having default)
    sync_rule = MagicMock()
    sync_rule.name = "some-excluded-rule"
    sync_rule.tags = ["sync", "default", "gobby"]
    sync_rule.definition_json = None

    # A worker-safety rule (should be included)
    safety_rule = MagicMock()
    safety_rule.name = "block-git-push"
    safety_rule.tags = ["worker-safety", "gobby"]
    safety_rule.definition_json = None

    result = resolve_rules_for_agent(agent, [default_rule, sync_rule, safety_rule])
    assert "require-task-before-edit" in result
    assert "block-git-push" in result
    assert "some-excluded-rule" not in result


def test_rule_matches_agent_applies_explicit_include_and_selector_exclude() -> None:
    agent = MagicMock()
    agent.workflows.rules = ["explicit-rule"]
    agent.workflows.rule_selectors = MagicMock()
    agent.workflows.rule_selectors.include = ["tag:default"]
    agent.workflows.rule_selectors.exclude = ["tag:sync"]

    explicit = MagicMock()
    explicit.name = "explicit-rule"
    explicit.tags = []
    explicit.definition_json = None

    selected = MagicMock()
    selected.name = "selected-rule"
    selected.tags = ["default"]
    selected.definition_json = None

    excluded = MagicMock()
    excluded.name = "excluded-rule"
    excluded.tags = ["default", "sync"]
    excluded.definition_json = None

    assert rule_matches_agent(agent, explicit) is True
    assert rule_matches_agent(agent, selected) is True
    assert rule_matches_agent(agent, excluded) is False


def test_resolve_variables_with_exclude() -> None:
    agent = MagicMock()
    agent.workflows.variable_selectors = MagicMock()
    agent.workflows.variable_selectors.include = ["name:*"]
    agent.workflows.variable_selectors.exclude = ["name:internal-*"]

    var1 = MagicMock()
    var1.name = "public-var"
    var1.tags = []
    var1.definition_json = None

    var2 = MagicMock()
    var2.name = "internal-secret"
    var2.tags = []
    var2.definition_json = None

    result = resolve_variables_for_agent(agent, [var1, var2])
    assert result is not None
    assert "public-var" in result
    assert "internal-secret" not in result
