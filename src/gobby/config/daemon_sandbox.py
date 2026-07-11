"""Daemon-owned sandbox defaults for web chat and agents."""

from typing import Literal

from pydantic import BaseModel, Field

__all__ = ["DaemonOwnedSandboxConfig"]


class DaemonOwnedSandboxConfig(BaseModel):
    """Sandbox defaults for daemon-owned runtimes."""

    enabled: bool = Field(
        default=True,
        description="Enable sandboxing for daemon-owned runtimes in this category.",
    )
    mode: Literal["permissive", "restrictive"] = Field(
        default="permissive",
        description="Sandbox strictness level for daemon-owned runtimes.",
    )
    allow_network: bool = Field(
        default=True,
        description="Allow daemon-owned runtimes to access the network.",
    )
    extra_read_paths: list[str] = Field(
        default_factory=list,
        description="Additional filesystem paths to allow read access inside the sandbox.",
    )
    extra_write_paths: list[str] = Field(
        default_factory=list,
        description="Additional filesystem paths to allow write access inside the sandbox.",
    )
