"""Code-index preflight wrapper for isolated agent workspaces."""

from __future__ import annotations

from pathlib import Path

from gobby.agents.code_index import CodeIndexPreflightResult
from gobby.agents.code_index import ensure_isolation_code_index as _ensure_isolation_code_index


async def ensure_isolation_code_index(
    isolated_path: str,
    *,
    timeout: float = 120.0,
    database_url: str | None = None,
    daemon_bind_host: str | None = None,
    daemon_port: int | None = None,
    runtime_root: Path | None = None,
    config_probe_timeout: float = 5.0,
    search_smoke_timeout: float = 10.0,
) -> CodeIndexPreflightResult:
    """Run and verify gcode indexing inside an isolated workspace before spawn."""
    return await _ensure_isolation_code_index(
        isolated_path,
        timeout=timeout,
        database_url=database_url,
        daemon_bind_host=daemon_bind_host,
        daemon_port=daemon_port,
        runtime_root=runtime_root,
        config_probe_timeout=config_probe_timeout,
        search_smoke_timeout=search_smoke_timeout,
    )
