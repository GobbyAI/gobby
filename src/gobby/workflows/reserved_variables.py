"""Workflow variables owned by runtime enforcement and observers."""

import logging
from typing import TYPE_CHECKING, Any, Literal

# The rule effect that performs a variable write; only a set_variable effect of an
# installed row may write project_policy.
RuleVariableWriter = Literal["set_variable", "success_variable", "acknowledge_variable", "runtime"]

_MUTABLE_CONTAINERS = (dict, list, set)


class RuleVariableAliasError(RuntimeError):
    """A rule write reused a stored container that the rule context cannot see change."""


HANDOFF_TURN_END_PENDING_VARIABLE = "_handoff_turn_end_pending"
# The Unix-socket root an SRT launch granted its own run; only the launch records it.
SRT_RUN_SOCKET_ROOT_VARIABLE = "_agent_srt_socket_root"
# Full SHAs of the two-parent merges a session authored; a base-merge candidate binds to one.
AUTHORED_MERGES_VARIABLE = "_authored_merge_commits"

if TYPE_CHECKING:
    from gobby.storage.definitions.rules import RuleDefinitionRow

RESERVED_WORKFLOW_VARIABLES = frozenset(
    {
        # Names the session's agent definition, which selects its rules and
        # send_message target modes; only spawn and persona application set it.
        "_agent_type",
        # An installed policy projection; session writes cannot alter authority.
        "project_policy",
        AUTHORED_MERGES_VARIABLE,
        "_block_reasons_shown",
        HANDOFF_TURN_END_PENDING_VARIABLE,
        SRT_RUN_SOCKET_ROOT_VARIABLE,
        "consecutive_tool_blocks",
        # Claim-sweep shields: each keeps an owner's claims through an absence,
        # so only their dedicated writers may place or clear them.
        "contested_terminal_expiry",
        "handoff_compact_continue_pending",
        "handoff_pull_pending",
        "operator_claim_hold",
        # Operator approval of exact unowned dirty paths; only the operator route writes it.
        "operator_dirty_path_approvals",
        # Edit ledgers the foreign-dirty edit gate trusts; only the edit observer writes them.
        "session_dirty_file_checkouts",
        "session_dirty_files",
        "session_unclaimed_edits",
        "git_operation_ownership",
        # Closure review evidence is authoritative across implementer/coordinator sessions.
        "_memory_task_review_records",
        "_memory_pending_task_reviews",
        "_memory_review_stop_delivered",
        "task_edited_file_checkouts",
        "grok_pending_briefing",
        "grok_pending_delivery",
        "grok_pending_turn_context",
        "listed_servers",
        "max_consecutive_blocked_tool_attempts",
        "open_tool_errors",
        "servers_listed",
        "step_workflow_complete",
        "_agent_early_exit_step",
        "tool_block_pending",
        "turn_interrupt_initiated",
        "unlocked_tools",
    }
)

RESERVED_WORKFLOW_VARIABLE_PREFIXES = (
    "_agent_workspace_",
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


def rule_variable_is_writable(
    name: str,
    row: "RuleDefinitionRow | None",
    *,
    writer: RuleVariableWriter = "runtime",
) -> bool:
    """Admit a target before evaluating its value or changing session state.

    Only trusted installed Gobby rules write runtime-managed names. An installed row may
    also write ``project_policy``, but only through its own set_variable effect; its
    mcp_call success_variable and block acknowledge_variable never reach the policy.
    """
    if is_reserved_workflow_variable(name) and not (
        row is not None
        and (
            is_internal_rule(row)
            or (name == "project_policy" and row.source == "installed" and writer == "set_variable")
        )
    ):
        logging.getLogger(__name__).warning(
            "Rule effect cannot write runtime-managed variable %r", name
        )
        return False
    return True


def write_rule_variable(
    variables: dict[str, Any],
    name: str,
    value: Any,
    *,
    row: "RuleDefinitionRow | None",
    delivery: str = "eager",
    staged: dict[str, Any] | None = None,
    writer: RuleVariableWriter = "runtime",
) -> bool:
    """Admit every rule-originated variable write before mutation or receipt staging."""
    if not rule_variable_is_writable(name, row, writer=writer):
        return False
    # The rule context refreshes a variable only when its object identity changes, so a
    # container mutated in place and written back would leave the context stale.
    if isinstance(value, _MUTABLE_CONTAINERS) and variables.get(name) is value:
        raise RuleVariableAliasError(
            f"Rule variable {name!r} was rewritten with the container it already holds; "
            "write a replacement value so the rule context refreshes."
        )
    variables[name] = value
    if delivery == "on_receipt" and staged is not None:
        staged[name] = value
    return True
