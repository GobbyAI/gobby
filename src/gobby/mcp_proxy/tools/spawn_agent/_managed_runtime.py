"""Validation helpers for trusted managed-runtime spawn restrictions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from gobby.agents.spawn_models import ManagedRuntimeProfile


def managed_runtime_pair_error(
    profile: ManagedRuntimeProfile | None,
    bind_authority: Callable[[str], None] | None,
) -> str | None:
    if (profile is None) != (bind_authority is None):
        return "managed runtime profile and authority callback must be paired"
    return None


def managed_runtime_selection_error(
    profile: ManagedRuntimeProfile | None,
    *,
    isolation: str,
    provider: str,
) -> str | None:
    if profile is None:
        return None
    if isolation != "none":
        return "managed runtime requires isolation='none'"
    if profile.provider != provider:
        return "managed runtime provider does not match resolved provider"
    return None


def managed_runtime_path_error(
    profile: ManagedRuntimeProfile | None,
    path: str,
) -> str | None:
    if profile is None:
        return None
    if Path(path).resolve() != Path(profile.scratch_root).resolve():
        return "managed runtime project path does not match immutable scratch root"
    return None


__all__ = [
    "managed_runtime_pair_error",
    "managed_runtime_path_error",
    "managed_runtime_selection_error",
]
