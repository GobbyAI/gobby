"""Explicit rule-selection defaults for tests unrelated to agent configuration."""

from typing import Any

from gobby.workflows.definitions import AgentDefinitionBody, AgentSelector, AgentWorkflows


def make_agent_workflows(**fields: Any) -> AgentWorkflows:
    fields.setdefault("rule_selectors", AgentSelector(include=[], exclude=[]))
    return AgentWorkflows(**fields)


def make_agent_definition(**fields: Any) -> AgentDefinitionBody:
    fields.setdefault("workflows", make_agent_workflows())
    return AgentDefinitionBody(**fields)
