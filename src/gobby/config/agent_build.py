"""Agent build-output location configuration."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AgentBuildConfig(BaseModel):
    """Where agent Cargo build folders are written."""

    build_root: str = Field(
        default="",
        description=(
            "Absolute, canonical, existing directory that holds agent Cargo build folders. "
            "Empty uses the Gobby cache. Takes effect at daemon startup."
        ),
    )
