"""Code-index preflight wrapper for isolated agent workspaces."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from gobby.agents.code_index import CodeIndexPreflightResult
from gobby.agents.code_index import ensure_isolation_code_index as _ensure_isolation_code_index

if TYPE_CHECKING:
    from gobby.storage.managed_credentials import ManagedCredential


async def ensure_isolation_code_index(
    isolated_path: str,
    *,
    timeout: float = 120.0,
    credential: ManagedCredential | None = None,
    runtime_root: Path | None = None,
    config_probe_timeout: float = 5.0,
    search_smoke_timeout: float = 10.0,
    api_token: str | None = None,
) -> CodeIndexPreflightResult:
    """Run and verify gcode indexing inside an isolated workspace before spawn."""
    return await _ensure_isolation_code_index(
        isolated_path,
        timeout=timeout,
        credential=credential,
        runtime_root=runtime_root,
        config_probe_timeout=config_probe_timeout,
        search_smoke_timeout=search_smoke_timeout,
        api_token=api_token,
    )
