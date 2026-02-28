import fnmatch
import json
from typing import Any

from gobby.storage.skills import Skill
from gobby.storage.workflow_definitions import WorkflowDefinitionRow
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
    dim: str, val: str, rule: WorkflowDefinitionRow, definition_json: dict[str, Any]
) -> bool:
    if dim == "*":
        return True
    if dim == "name":
        return fnmatch.fnmatch(rule.name, val)
    if dim == "source":
        return fnmatch.fnmatch(rule.source, val)
    if dim == "tag":
        return any(fnmatch.fnmatch(t, val) for t in (rule.tags or []))
    if dim == "group":
        return fnmatch.fnmatch(definition_json.get("group", ""), val)
    return False


def resolve_rules_for_agent(
    agent: AgentDefinitionBody, all_rules: list[WorkflowDefinitionRow]
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

    include_matches = set()
    exclude_matches = set()
    selectors = agent.workflows.rule_selectors

    for rule in all_rules:
        definition_json: dict[str, Any] = {}
        if rule.definition_json:
            try:
                definition_json = json.loads(rule.definition_json)
            except (json.JSONDecodeError, TypeError):
                pass

        for inc in selectors.include:
            dim, val = parse_selector(inc)
            if _match_rule(dim, val, rule, definition_json):
                include_matches.add(rule.name)
                break

        for exc in selectors.exclude:
            dim, val = parse_selector(exc)
            if _match_rule(dim, val, rule, definition_json):
                exclude_matches.add(rule.name)
                break

    combined = explicit | include_matches
    return combined - exclude_matches


def _match_skill(dim: str, val: str, skill: Skill) -> bool:
    if dim == "*":
        return True
    if dim == "name":
        return fnmatch.fnmatch(skill.name, val)
    if dim == "source":
        source_type = str(skill.source_type) if skill.source_type else ""
        return fnmatch.fnmatch(source_type, val)
    if dim == "category":
        cat = ""
        if skill.metadata and isinstance(skill.metadata, dict):
            cat = skill.metadata.get("skillport", {}).get("category", "") or skill.metadata.get(
                "gobby", {}
            ).get("category", "")
        return fnmatch.fnmatch(cat, val)
    if dim == "tag":
        tags: list[str] = []
        if skill.metadata and isinstance(skill.metadata, dict):
            tags.extend(skill.metadata.get("gobby", {}).get("tags", []))
            tags.extend(skill.metadata.get("skillport", {}).get("tags", []))
        return any(fnmatch.fnmatch(t, val) for t in tags)
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
    agent: AgentDefinitionBody, all_variables: list[WorkflowDefinitionRow]
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
        if var.definition_json:
            try:
                definition_json = json.loads(var.definition_json)
            except (json.JSONDecodeError, TypeError):
                pass

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
