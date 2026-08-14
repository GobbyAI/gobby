import fnmatch
import json
from typing import Any

from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.storage.skills import Skill
from gobby.workflows.definitions import AgentDefinitionBody


def parse_selector(s: str) -> tuple[str, str]:
    """Parse a selector string into (dimension, value).

    Format: 'tag:X', 'group:X', 'name:X', 'source:X', 'category:X', or '*'.
    Bare strings default to name matching.
    """
    KNOWN_PREFIXES = {"tag", "group", "name", "source", "category"}
    if ":" in s:
        prefix, _, value = s.partition(":")
        if prefix in KNOWN_PREFIXES:
            return prefix, value
    return "name", s


def _match_rule(
    dim: str, val: str, rule: RuleDefinitionRow, definition_json: dict[str, Any]
) -> bool:
    if dim == "*":
        return True
    if dim == "name":
        return fnmatch.fnmatchcase(rule.name, val)
    if dim == "source":
        return fnmatch.fnmatchcase(rule.source, val)
    if dim == "tag":
        return any(fnmatch.fnmatchcase(t, val) for t in (rule.tags or []))
    if dim == "group":
        return fnmatch.fnmatchcase(definition_json.get("group", ""), val)
    if dim == "category":
        return fnmatch.fnmatchcase(definition_json.get("category", ""), val)
    return False


def resolve_rules_for_agent(
    agent: AgentDefinitionBody, all_rules: list[RuleDefinitionRow]
) -> set[str]:
    """Resolve active rules for an agent, combining explicit rules and selectors.

    1. Gather explicit workflows.rules
    2. Gather selector include matches
    3. Union of 1 + 2
    4. Subtract exclude matches from the entire set
    """
    explicit = set(agent.workflows.rules)
    if not agent.workflows.rule_selectors:
        return explicit

    active = set(explicit)
    for rule in all_rules:
        definition_json = _rule_definition_json(rule)
        excluded = _rule_excluded_by_agent(agent, rule, definition_json=definition_json)
        if excluded:
            active.discard(rule.name)
        elif rule_matches_agent(
            agent,
            rule,
            definition_json=definition_json,
            excluded=excluded,
        ):
            active.add(rule.name)
    return active


def rule_matches_agent(
    agent: AgentDefinitionBody,
    rule: RuleDefinitionRow,
    *,
    definition_json: dict[str, Any] | None = None,
    excluded: bool | None = None,
) -> bool:
    """Return whether a single rule row is active for an agent definition."""
    explicit = set(agent.workflows.rules)
    selectors = agent.workflows.rule_selectors

    if selectors is None:
        return rule.name in explicit

    rule_definition_json = (
        _rule_definition_json(rule) if definition_json is None else definition_json
    )

    if excluded is None:
        excluded = _rule_excluded_by_agent(
            agent,
            rule,
            definition_json=rule_definition_json,
        )
    if excluded:
        return False

    if rule.name in explicit:
        return True

    for inc in selectors.include:
        dim, val = parse_selector(inc)
        if _match_rule(dim, val, rule, rule_definition_json):
            return True

    return False


def _rule_excluded_by_agent(
    agent: AgentDefinitionBody,
    rule: RuleDefinitionRow,
    *,
    definition_json: dict[str, Any] | None = None,
) -> bool:
    selectors = agent.workflows.rule_selectors
    if selectors is None:
        return False

    rule_definition_json = (
        _rule_definition_json(rule) if definition_json is None else definition_json
    )
    for exc in selectors.exclude:
        dim, val = parse_selector(exc)
        if _match_rule(dim, val, rule, rule_definition_json):
            return True
    return False


def _rule_definition_json(rule: RuleDefinitionRow) -> dict[str, Any]:
    payload = rule.definition_json
    if isinstance(payload, dict):
        return payload
    if not payload:
        return {}
    try:
        definition_json = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return {}
    return definition_json if isinstance(definition_json, dict) else {}


def _match_skill(dim: str, val: str, skill: Skill) -> bool:
    if dim == "*":
        return True
    if dim == "name":
        return fnmatch.fnmatchcase(skill.name, val)
    if dim == "source":
        source_type = str(skill.source_type) if skill.source_type else ""
        return fnmatch.fnmatchcase(source_type, val)
    if dim == "category":
        cat = ""
        if skill.metadata and isinstance(skill.metadata, dict):
            cat = skill.metadata.get("skillport", {}).get("category", "") or skill.metadata.get(
                "gobby", {}
            ).get("category", "")
        return fnmatch.fnmatchcase(cat, val)
    if dim == "tag":
        tags: list[str] = []
        if skill.metadata and isinstance(skill.metadata, dict):
            tags.extend(skill.metadata.get("gobby", {}).get("tags", []))
            tags.extend(skill.metadata.get("skillport", {}).get("tags", []))
        return any(fnmatch.fnmatchcase(t, val) for t in tags)
    return False


def resolve_skills_for_agent(
    agent: AgentDefinitionBody, all_skills: list[Skill]
) -> set[str] | None:
    """Resolve active skills for an agent using skill_selectors.

    Returns None if skill_selectors is null (permissive by default).
    Returns a set of skill names if selectors are configured.
    """
    selectors = agent.workflows.skill_selectors
    if selectors is None:
        return None

    include_matches = set()
    exclude_matches = set()

    for skill in all_skills:
        for inc in selectors.include:
            dim, val = parse_selector(inc)
            if _match_skill(dim, val, skill):
                include_matches.add(skill.name)
                break

        for exc in selectors.exclude:
            dim, val = parse_selector(exc)
            if _match_skill(dim, val, skill):
                exclude_matches.add(skill.name)
                break

    return include_matches - exclude_matches


def resolve_variables_for_agent(
    agent: AgentDefinitionBody, all_variables: list[Any]
) -> set[str] | None:
    """Resolve active variable definitions for an agent using variable_selectors.

    Returns None if variable_selectors is null (loads all enabled session defaults).
    Returns a set of variable definition names if selectors are configured.
    """
    selectors = agent.workflows.variable_selectors
    if selectors is None:
        return None

    include_matches = set()
    exclude_matches = set()

    for var in all_variables:
        definition_json: dict[str, Any] = {}
        payload = getattr(var, "definition_json", None)
        if payload:
            try:
                parsed = json.loads(payload) if isinstance(payload, str) else payload
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            if isinstance(parsed, dict):
                definition_json = parsed

        for inc in selectors.include:
            dim, val = parse_selector(inc)
            if _match_rule(dim, val, var, definition_json):
                include_matches.add(var.name)
                break

        for exc in selectors.exclude:
            dim, val = parse_selector(exc)
            if _match_rule(dim, val, var, definition_json):
                exclude_matches.add(var.name)
                break

    return include_matches - exclude_matches
