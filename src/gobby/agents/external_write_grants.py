"""Explicit, audited external workspace grants for managed agent runs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.agents.sandbox import SandboxConfig
from gobby.agents.sandbox_policy import sensitive_roots, sensitive_write_roots

GRANT_KEY = "external_write_grant"


def apply_write_grant(config: SandboxConfig, grant: dict[str, Any] | None) -> SandboxConfig:
    """Merge the explicit request without changing daemon sandbox policy."""
    if grant is None:
        return config
    paths = list(dict.fromkeys([*config.extra_write_paths, *grant["canonical_roots"]]))
    return config.model_copy(update={"extra_write_paths": paths})


def canonical_write_roots(paths: object, reason: object) -> list[str]:
    """Validate every request, retaining stable canonical order."""
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError("extra_write_paths must be a list of absolute directory paths")
    if paths and (not isinstance(reason, str) or not reason.strip()):
        raise ValueError("Nonempty extra_write_paths require a nonblank write_paths_reason")
    protected = [Path(path).resolve() for path in (*sensitive_roots(), *sensitive_write_roots())]
    roots: list[str] = []
    for requested in paths:
        path = Path(requested)
        if not path.is_absolute():
            raise ValueError(f"External write root must be absolute: {requested!r}")
        try:
            canonical = path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"External write root cannot be resolved: {requested!r}") from exc
        if not canonical.is_dir():
            raise ValueError(f"External write root must be an existing directory: {requested!r}")
        if canonical == Path(canonical.anchor) or canonical == Path.home().resolve():
            raise ValueError(f"Filesystem and home roots cannot be granted: {requested!r}")
        if any(
            canonical.is_relative_to(root) or root.is_relative_to(canonical) for root in protected
        ):
            raise ValueError(f"External write root overlaps a protected root: {requested!r}")
        if str(canonical) not in roots:
            roots.append(str(canonical))
    return roots


def revalidate_write_grant(metadata: dict[str, Any]) -> list[str]:
    """Fail closed when a recorded request now resolves to a different grant."""
    grant = metadata.get(GRANT_KEY)
    if grant is None:
        return []
    if not isinstance(grant, dict):
        raise ValueError("Invalid external write grant metadata")
    roots = canonical_write_roots(grant.get("requested_roots"), grant.get("reason"))
    if roots != grant.get("canonical_roots"):
        raise ValueError("External write grant canonical identity changed")
    return roots


def authorize_write_grant(
    paths: list[str] | None,
    reason: str | None,
    *,
    caller_session_id: str | None,
    parent_session_id: str | None,
    session_manager: Any,
    run_storage: Any,
) -> dict[str, Any] | None:
    """A coordinator asserts authority; managed callers may only narrow their grant."""
    roots = canonical_write_roots([] if paths is None else paths, reason)
    if not roots:
        return None
    asserting_id = caller_session_id or parent_session_id
    if not asserting_id or session_manager is None:
        raise ValueError("External write grants require a recorded asserting session")
    session = session_manager.get(asserting_id)
    if session is None:
        raise ValueError("External write grant asserting session was not found")
    run_id = getattr(session, "agent_run_id", None)
    run = run_storage.get(run_id) if run_id else run_storage.get_by_session(session.id)
    if run is not None:
        if run.child_session_id != session.id:
            raise ValueError("External write grant session/run relationship is inconsistent")
        allowed = revalidate_write_grant(run.resume_metadata_json or {})
        if any(
            not any(Path(root).is_relative_to(Path(base)) for base in allowed) for root in roots
        ):
            raise ValueError(
                "Managed children may only delegate recorded external roots or subdirectories"
            )
        run_id = run.id
    elif run_id or getattr(session, "parent_session_id", None):
        raise ValueError("Managed session has no recorded run for external write delegation")
    return {
        "requested_roots": list(paths or []),
        "canonical_roots": roots,
        "reason": reason.strip() if reason else "",
        "asserting_session_id": session.id,
        "parent_run_id": run_id,
        "asserted_at": datetime.now(UTC).isoformat(),
    }
