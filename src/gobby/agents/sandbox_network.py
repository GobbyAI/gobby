"""Resolve an agent definition's ``network`` into its spawn sandbox config."""

from __future__ import annotations

from typing import Any, get_args

from gobby.agents.sandbox import SandboxConfig, agent_sandbox_config
from gobby.agents.sandbox_domains import trusted_domains
from gobby.agents.sandbox_gate import SandboxRequiredError
from gobby.workflows.agent_models import AgentDefinitionBody

_NETWORK_PROFILES: tuple[str, ...] = get_args(
    AgentDefinitionBody.model_fields["network"].annotation
)


def apply_network_override(
    agent_body: AgentDefinitionBody | None, network: str | None
) -> AgentDefinitionBody | None:
    """The final definition with ``network`` as its profile for one launch.

    ``None`` inherits the definition's profile. An override returns a copy, so the
    loaded body and the stored definition keep theirs. ``model_copy`` skips
    validation, so the profile is checked here.
    """
    if network is None:
        return agent_body
    if network not in _NETWORK_PROFILES:
        raise ValueError(f"network must be one of {', '.join(_NETWORK_PROFILES)}, got {network!r}")
    if agent_body is None:
        raise ValueError(f"network {network!r} needs a resolved agent definition to override")
    return agent_body.model_copy(update={"network": network})


def definition_sandbox_config(
    daemon_config: Any | None, agent_body: AgentDefinitionBody | None
) -> SandboxConfig:
    """The ``agent_sandbox`` policy, widened by the Trusted allowlist for a ``trusted`` body.

    ``trusted`` adds the vendored seed plus the git and package-registry groups;
    ``allow_network`` stays false, so egress remains an SRT allowlist. Only a
    ``trusted`` body reads the seed, and an unreadable seed refuses the spawn.
    """
    base = agent_sandbox_config(daemon_config)
    if agent_body is None or agent_body.network != "trusted":
        return base
    try:
        seed = trusted_domains()
    except (OSError, ValueError) as exc:
        raise SandboxRequiredError(f"the vendored Trusted seed is unreadable: {exc}") from exc
    return base.model_copy(
        update={
            "allowed_domains": [*base.allowed_domains, *seed],
            "allow_git_network": True,
            "allow_package_registries": True,
        }
    )
