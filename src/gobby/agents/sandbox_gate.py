"""The one check that every spawn_agent launch and resume runs under managed SRT."""

from __future__ import annotations

from typing import Any

from gobby.agents.sandbox import SandboxConfig, coerce_sandbox_config
from gobby.agents.srt_runtime import SrtRuntimeError, verify_srt_installation

__all__ = [
    "SandboxRequiredError",
    "require_managed_srt",
    "resolve_resume_sandbox",
    "verify_srt_installation",
]


class SandboxRequiredError(RuntimeError):
    """The effective config would launch a provider outside managed SRT."""


def require_managed_srt(config: SandboxConfig) -> SandboxConfig:
    """Return `config` only when it is enabled `srt` and the pinned install verifies."""
    if not (config.enabled and config.backend == "srt"):
        raise SandboxRequiredError(
            f"spawn_agent requires managed SRT (enabled={config.enabled}, backend={config.backend})"
        )
    try:
        verify_srt_installation()
    except (OSError, SrtRuntimeError) as exc:
        raise SandboxRequiredError(f"managed SRT is unavailable: {exc}") from exc
    return config


def resolve_resume_sandbox(resume_metadata: dict[str, Any]) -> SandboxConfig:
    """Gate the run's snapshot config; a run without one never resumes."""
    config = coerce_sandbox_config(resume_metadata.get("sandbox_config"))
    if config is None:
        raise SandboxRequiredError("resumed run has no sandbox snapshot")
    return require_managed_srt(config)
