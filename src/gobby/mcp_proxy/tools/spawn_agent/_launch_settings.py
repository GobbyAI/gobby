"""Launch settings both spawn paths resolve the same way: model, reasoning, and run lifetime.

A run on another machine launches with these resolved values, not with the raw request
fields, so its definition and its resumed session decide the same launch as a local run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.agents.model_defaults import (
    ModelChoice,
    ModelDefaults,
    PolicyEntry,
    is_denylisted,
    model_defaults_from_policy,
)
from gobby.agents.reasoning import SpawnReasoningResolution, resolve_spawn_reasoning
from gobby.mcp_proxy.tools.spawn_agent._provider_resolution import (
    incompatible_spawn_model_provider_after_recollect,
)
from gobby.mcp_proxy.tools.spawn_agent._run_lifetime import RunLifetime
from gobby.mcp_proxy.tools.spawn_agent._runtime import _normalize_optional_model
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.tasks.closure_snapshot import installed_project_policy

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.session_models import Session
    from gobby.workflows.definitions import AgentDefinitionBody

# A launch that names no reasoning effort from its spawn or its definition runs at this level.
DEFAULT_LAUNCH_REASONING_EFFORT = "medium"

# The bundled agent that has no model by design. Its unpinned spawns refuse with guidance.
_DEFAULT_AGENT = "default"

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
    model_chain: tuple[ModelChoice, ...] = ()


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
    model_defaults: ModelDefaults | None = None,
) -> LaunchModel:
    """Resolve the run's model chain and reasoning for ``provider``. Raises ``LaunchRefused``.

    The model is the first of: the spawn's ``model``; the policy map entry for this agent, when
    the entry names ``provider``; the definition's model as declared; otherwise a refusal. A
    model that ``provider`` does not serve is refused, never substituted. A denylisted policy or
    definition model is refused, while an explicit spawn model is not checked. ``model_chain``
    is the primary choice, then the fallbacks of whichever source supplied the primary: the
    policy entry, else the definition. An explicit spawn model takes no fallbacks.
    """
    agent_name = agent_body.name if agent_body is not None else None
    owner = f"agent {agent_name!r}" if agent_body else "this spawn"
    explicit_model = _normalize_optional_model(model)
    if explicit_model is None and agent_name == _DEFAULT_AGENT:
        raise LaunchRefused(
            {
                "success": False,
                "error": (
                    "The default agent has no model by design. Pass provider=, model= and "
                    "reasoning_effort= on the spawn; an unpinned spawn of default is refused."
                ),
            }
        )
    policy_entry: PolicyEntry | None = None
    if explicit_model is None and model_defaults is not None:
        candidate = model_defaults.entry_for(agent_name)
        if candidate is not None and candidate.provider == provider:
            policy_entry = candidate
    effective_model = explicit_model if policy_entry is None else policy_entry.model
    if effective_model is None and agent_body:
        # The definition's model runs as declared. A provider that does not serve it is
        # refused by the compatibility check below, never swapped for a profile peer.
        effective_model = _normalize_optional_model(agent_body.model)
    if effective_model is None:
        raise LaunchRefused(
            {
                "success": False,
                "error": (
                    f"No model for the {provider} launch of {owner}. Pass model= on the spawn, "
                    "add an agent_model_defaults entry for it in project policy, or set model: "
                    "on the agent definition. A launch without a model would run the CLI's "
                    "default model, so it is refused."
                ),
            }
        )
    if explicit_model is None and model_defaults is not None:
        if is_denylisted(effective_model, model_defaults.denylist):
            raise LaunchRefused(
                {
                    "success": False,
                    "error": (
                        f"Model {effective_model!r} is on the default_model_denylist, so it "
                        f"cannot be the default for the {provider} launch of {owner}. Pass "
                        "model= on the spawn to run it explicitly."
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
    if not requested_effort and policy_entry is not None:
        requested_effort = policy_entry.reasoning_effort
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
    fallbacks: tuple[ModelChoice, ...] = ()
    if explicit_model is None and policy_entry is not None:
        fallbacks = policy_entry.fallbacks
    elif explicit_model is None and agent_body is not None:
        fallbacks = tuple(
            ModelChoice(
                provider=fallback.provider,
                model=fallback.model,
                reasoning_effort=fallback.reasoning_effort,
            )
            for fallback in agent_body.fallbacks
        )
    if model_defaults is not None:
        for fallback in fallbacks:
            if is_denylisted(fallback.model, model_defaults.denylist):
                raise LaunchRefused(
                    {
                        "success": False,
                        "error": (
                            f"Fallback model {fallback.model!r} for {owner} is on the "
                            "default_model_denylist. Remove it from the agent's map entry."
                        ),
                    }
                )
    chain = (
        ModelChoice(provider=provider, model=effective_model, reasoning_effort=requested_effort),
        *(
            ModelChoice(
                provider=fallback.provider,
                model=fallback.model,
                reasoning_effort=fallback.reasoning_effort or requested_effort,
            )
            for fallback in fallbacks
        ),
    )
    return LaunchModel(
        model=effective_model,
        reasoning_effort=requested_effort,
        reasoning_required=effective_required,
        reasoning=reasoning,
        model_chain=chain,
    )


def agent_provider_for_spawn(
    agent_body: AgentDefinitionBody | None,
    model: str | None,
    model_defaults: ModelDefaults | None,
) -> str | None:
    """The provider a spawn resolves before its model: the map entry's, else the definition's.

    The map entry applies only when the spawn names no model, the same condition under which
    the resolver consults the map. A provider on the spawn itself still wins over both.
    """
    if agent_body is None:
        return None
    if _normalize_optional_model(model) is None and model_defaults is not None:
        entry = model_defaults.entry_for(agent_body.name)
        if entry is not None:
            return entry.provider
    return agent_body.provider


def load_model_defaults(db: HubDatabase | None, project_id: str | None) -> ModelDefaults:
    """The project's model policy, read from its installed ``project_policy``.

    A policy that cannot be read (conflicting or malformed installed writers) raises
    ``ValueError``. The caller refuses the launch rather than falling back to a definition's
    model. Without a database there is no policy to read, so the defaults are empty.
    """
    if db is None:
        return ModelDefaults()
    manager = RuleDefinitionManager(db)
    policy = installed_project_policy(
        manager.list_effective(project_id),
        provenance=manager.list_all(project_id=project_id),
    )
    if policy is None:
        raise ValueError(
            "project_policy cannot be read: its installed writers conflict or are malformed"
        )
    return model_defaults_from_policy(policy)


def standing_seat_suffix(run_lifetime: RunLifetime) -> str:
    """The prompt suffix an interactive standing seat carries; empty for a one-shot run."""
    return STANDING_SEAT_SUFFIX if run_lifetime.execution_mode == "interactive" else ""
