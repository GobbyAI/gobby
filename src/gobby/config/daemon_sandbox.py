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
    backend: Literal["srt"] = Field(
        default="srt",
        description="Managed Sandbox Runtime backend.",
    )
    mode: Literal["permissive", "restrictive"] = Field(
        default="permissive",
        description="Sandbox strictness level for daemon-owned runtimes.",
    )
    allow_network: bool = Field(
        default=False,
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
    extra_deny_read_paths: list[str] = Field(
        default_factory=list,
        description="Additional filesystem paths hidden from sandboxed processes.",
    )
    extra_deny_write_paths: list[str] = Field(
        default_factory=list,
        description="Additional write-deny paths inside otherwise writable roots.",
    )
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="Additional outbound domains allowed by the SRT backend.",
    )
    denied_domains: list[str] = Field(
        default_factory=list,
        description="Outbound domains denied by the SRT backend.",
    )
    allow_git_network: bool = Field(
        default=False,
        description="Allow Git forge network endpoints for push, pull, and fetch.",
    )
    allow_package_registries: bool = Field(
        default=False,
        description="Allow package-registry endpoints and their local caches.",
    )
    allow_unix_sockets: list[str] = Field(
        default_factory=list,
        description="Exact Unix socket paths allowed by the SRT backend.",
    )
