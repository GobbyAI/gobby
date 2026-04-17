"""Per-CLI runtime sandbox defaults."""

from typing import Literal

from pydantic import BaseModel, Field

__all__ = ["CLISandboxConfig", "CLISandboxProviderConfig"]


class CLISandboxProviderConfig(BaseModel):
    """Runtime sandbox defaults for a single CLI provider."""

    enabled: bool = Field(
        default=True,
        description="Enable the provider's built-in sandbox for daemon-managed sessions.",
    )
    mode: Literal["permissive", "restrictive"] = Field(
        default="permissive",
        description="Sandbox strictness profile for this provider.",
    )
    allow_network: bool = Field(
        default=True,
        description="Allow external network access when this provider runs sandboxed.",
    )
    extra_read_paths: list[str] = Field(
        default_factory=list,
        description="Additional filesystem paths to allow read access inside the sandbox.",
    )
    extra_write_paths: list[str] = Field(
        default_factory=list,
        description="Additional filesystem paths to allow write access inside the sandbox.",
    )


class CLISandboxConfig(BaseModel):
    """Runtime sandbox defaults keyed by CLI provider."""

    claude: CLISandboxProviderConfig = Field(
        default_factory=CLISandboxProviderConfig,
        description="Claude Code runtime sandbox defaults.",
    )
    codex: CLISandboxProviderConfig = Field(
        default_factory=CLISandboxProviderConfig,
        description="Codex CLI runtime sandbox defaults.",
    )
    gemini: CLISandboxProviderConfig = Field(
        default_factory=CLISandboxProviderConfig,
        description="Gemini CLI runtime sandbox defaults.",
    )
    qwen: CLISandboxProviderConfig = Field(
        default_factory=CLISandboxProviderConfig,
        description="Qwen Code runtime sandbox defaults.",
    )

    def for_provider(self, provider: str) -> CLISandboxProviderConfig | None:
        """Return sandbox defaults for a provider when available."""
        if provider not in {"claude", "codex", "gemini", "qwen"}:
            return None
        return getattr(self, provider, None)
