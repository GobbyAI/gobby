"""Workflow variables owned by runtime enforcement and observers."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.storage.definitions.rules import RuleDefinitionRow

RESERVED_WORKFLOW_VARIABLES = frozenset(
    {
        "_block_reasons_shown",
        "consecutive_tool_blocks",
        "listed_servers",
        "max_consecutive_blocked_tool_attempts",
        "open_tool_errors",
        "servers_listed",
        "step_workflow_complete",
        "tool_block_pending",
        "unlocked_tools",
    }
)

RESERVED_WORKFLOW_VARIABLE_PREFIXES = (
    "_last_blocked_",
    "edit_write_",
    "enforce_",
)


def is_reserved_workflow_variable(name: str) -> bool:
    """Return whether a variable may only be written by trusted runtime code."""
    return name in RESERVED_WORKFLOW_VARIABLES or name.startswith(
        RESERVED_WORKFLOW_VARIABLE_PREFIXES
    )


def is_internal_rule(row: "RuleDefinitionRow") -> bool:
    """Return whether a rule comes from Gobby's trusted installed definitions."""
    tags = row.tags or []
    return (
        row.source == "installed"
        and row.project_id is None
        and "gobby" in tags
        and "user" not in tags
    )
