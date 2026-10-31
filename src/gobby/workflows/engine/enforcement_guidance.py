"""Denial guidance text for agent, step and execution-wrapper tool blocks."""

from __future__ import annotations

from typing import Any

from gobby.workflows.definitions import WorkflowStep
from gobby.workflows.engine.skill_load_guidance import (
    skill_load_block_guidance,
    skill_load_call_lead,
)


def step_tool_block_guidance(step_name: str) -> str:
    claim = " Call gobby-tasks:claim_task to leave claim." if step_name == "claim" else ""
    return (
        f"\nThis capability is unavailable for the rest of the '{step_name}' step.{claim} "
        "Abandon this call, continue with an allowed operation, and do not retry "
        "the same blocked tool in this step. Repeating an identical denial three "
        "times ends an autonomous run in a terminal blocked state."
    )


def step_tool_denial_reason(
    wf_name: str,
    step: WorkflowStep,
    variables: dict[str, Any],
    detail: str,
) -> str:
    """Lead a skill-step denial with its unloaded targets; the boilerplate follows."""
    return (
        f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
        f"{skill_load_call_lead(step, variables)}"
        f"{detail}"
        f"{skill_load_block_guidance(step, variables)}"
        f"{step_tool_block_guidance(step.name)}"
    )


def agent_tool_block_guidance() -> str:
    return (
        "\nAbandon this call and continue with an operation permitted by the agent "
        "definition. Do not retry the same denied target. Repeating an identical "
        "denial three times ends an autonomous run in a terminal blocked state."
    )


def execution_wrapper_guidance(tool_name: str) -> str:
    """Name the direct call when a denial hits an execution-wrapper tool."""
    folded = tool_name.casefold()
    if "js_repl" not in folded and "cua_repl" not in folded:
        return ""
    return (
        f"\nTool '{tool_name}' is an execution wrapper. "
        "Call mcp__gobby__call_tool directly instead of the wrapper."
    )
