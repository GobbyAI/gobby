"""Resolve definition-owned run lifetime and spawn execution-mode overrides."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from gobby.workflows.agent_models import AgentDefinitionBody


@dataclass(frozen=True)
class RunLifetime:
    execution_mode: Literal["one_shot", "interactive"]
    idle_ttl_seconds: int | None


def resolve_run_lifetime(
    agent_body: AgentDefinitionBody | None,
    execution_mode: Literal["one_shot", "interactive"] | None,
) -> RunLifetime:
    effective_mode = (
        execution_mode
        if execution_mode is not None
        else (agent_body.execution_mode if agent_body is not None else "one_shot")
    )
    if effective_mode not in {"one_shot", "interactive"}:
        raise ValueError("execution_mode must be one_shot or interactive")
    ttl = agent_body.idle_ttl_seconds if agent_body is not None else None
    return RunLifetime(effective_mode, ttl if effective_mode == "interactive" else None)
