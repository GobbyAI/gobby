"""Launch settings both spawn paths resolve the same way: model, reasoning, and run lifetime.

A run on another machine launches with these resolved values, not with the raw request
fields, so its definition and its resumed session decide the same launch as a local run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.agents.provider_rotation import model_for_provider
from gobby.agents.reasoning import SpawnReasoningResolution, resolve_spawn_reasoning
from gobby.mcp_proxy.tools.spawn_agent._provider_resolution import (
    incompatible_spawn_model_provider_after_recollect,
)
from gobby.mcp_proxy.tools.spawn_agent._run_lifetime import RunLifetime
from gobby.mcp_proxy.tools.spawn_agent._runtime import _normalize_optional_model

if TYPE_CHECKING:
    from gobby.storage.session_models import Session
    from gobby.workflows.definitions import AgentDefinitionBody

# A launch that names no reasoning effort from its spawn or its definition runs at this level.
DEFAULT_LAUNCH_REASONING_EFFORT = "medium"

STANDING_SEAT_SUFFIX = (
    "\n\nThis is an interactive standing seat. Remain available between turns and tasks. "
    "An idle prompt, completed task, or finished runbook does not end this seat. "
    "Call gobby-agents:end_agent_run only when explicitly directed to end the seat."
)


class LaunchRefused(ValueError):
    """A launch setting that cannot run. ``response`` is the spawn response that reports it."""

    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__(str(response["error"]))
        self.response = response


@dataclass(frozen=True)
class LaunchModel:
    """The model and reasoning request a run launches with, as resolved for its provider."""

    model: str | None
    reasoning_effort: str | None
    reasoning_required: bool | None
    reasoning: SpawnReasoningResolution


def resume_launch_defaults(
    resume_target: Session, *, model: str | None, reasoning_effort: str | None
) -> tuple[str, str | None, str | None]:
    """A resumed seat launches on its session's provider, model and effort unless the caller names one."""
    return (
        resume_target.source,
        _normalize_optional_model(model) or _normalize_optional_model(resume_target.model),
        reasoning_effort or resume_target.reasoning_effort,
    )


async def resolve_launch_model(
    *,
    provider: str,
    model: str | None,
    agent_body: AgentDefinitionBody | None,
    reasoning_effort: str | None,
    reasoning_required: bool | None,
) -> LaunchModel:
    """Resolve the run's model and reasoning for ``provider``. Raises ``LaunchRefused``."""
    effective_model = _normalize_optional_model(model)
    if effective_model is None and agent_body:
        agent_model = _normalize_optional_model(agent_body.model)
        if agent_model is not None:
            # The agent names one model, but the provider it lands on need not be
            # the one that model belongs to: the spawn may override the provider,
            # and `provider: inherit` follows the spawning session. Resolve the
            # target provider's model at the same tier rather than leaving the
            # model unset for that CLI's configured default to fill in.
            effective_model = model_for_provider(
                target_provider=provider,
                declared_model=agent_model,
            )
            if effective_model is None:
                raise LaunchRefused(
                    {
                        "success": False,
                        "error": (
                            f"Agent {agent_body.name!r} declares model {agent_model!r}, which "
                            f"belongs to another provider and has no {provider} "
                            f"equivalent. Pass an explicit model for {provider} "
                            "instead of leaving it to the provider's default."
                        ),
                    }
                )
    if effective_model is None:
        owner = f"agent {agent_body.name!r}" if agent_body else "this spawn"
        raise LaunchRefused(
            {
                "success": False,
                "error": (
                    f"No model for the {provider} launch of {owner}. Pass model= on the spawn "
                    "or set model: on the agent definition. A launch without a model would "
                    "run the CLI's default model, so it is refused."
                ),
            }
        )
    pair_error = await incompatible_spawn_model_provider_after_recollect(
        provider=provider,
        model=effective_model,
    )
    if pair_error is not None:
        raise LaunchRefused(pair_error.to_response())

    requested_effort = reasoning_effort
    if not requested_effort and agent_body:
        requested_effort = agent_body.reasoning_effort
    if not requested_effort:
        requested_effort = DEFAULT_LAUNCH_REASONING_EFFORT
    effective_required = reasoning_required
    if effective_required is None and agent_body:
        effective_required = agent_body.reasoning_required
    reasoning = resolve_spawn_reasoning(
        provider=provider,
        model=effective_model,
        requested_effort=requested_effort,
        reasoning_required=effective_required,
    )
    if reasoning.reasoning_required and reasoning.effective_effort is None:
        raise LaunchRefused(
            {
                "success": False,
                "error": reasoning.message or "Requested reasoning is not supported",
                "reasoning": reasoning.to_dict(),
            }
        )
    return LaunchModel(
        model=effective_model,
        reasoning_effort=requested_effort,
        reasoning_required=effective_required,
        reasoning=reasoning,
    )


def standing_seat_suffix(run_lifetime: RunLifetime) -> str:
    """The prompt suffix an interactive standing seat carries; empty for a one-shot run."""
    return STANDING_SEAT_SUFFIX if run_lifetime.execution_mode == "interactive" else ""
