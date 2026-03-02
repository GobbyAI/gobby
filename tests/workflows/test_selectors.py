"""Tests for workflows/selectors.py."""

from __future__ import annotations

from unittest.mock import MagicMock

from gobby.workflows.selectors import (
    parse_selector,
    _match_rule,
    resolve_rules_for_agent,
    _match_skill,
    resolve_skills_for_agent,
    resolve_variables_for_agent,
)

import pytest

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


def test_match_skill_source_none() -> None:
    skill = MagicMock()
    skill.source_type = None
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
    s1.source_type = None

    s2 = MagicMock()
    s2.name = "dangerous"
    s2.metadata = None
    s2.source_type = None

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
