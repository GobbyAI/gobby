"""Daemon-owned sandbox defaults for web chat and agents."""

from pydantic import BaseModel, Field

__all__ = ["DaemonOwnedSandboxConfig"]


class DaemonOwnedSandboxConfig(BaseModel):
    """Sandbox defaults for daemon-owned runtimes."""

    enabled: bool = Field(
        default=True,
        description="Enable sandboxing for daemon-owned runtimes in this category.",
    )
    extra_read_paths: list[str] = Field(
        default_factory=list,
        description="Additional filesystem paths to allow read access inside the sandbox.",
    )
    extra_write_paths: list[str] = Field(
        default_factory=list,
        description="Additional filesystem paths to allow write access inside the sandbox.",
    )
