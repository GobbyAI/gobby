"""Provider-aware path policy for structured plan-mode scratch writes."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from gobby.providers.registry import provider_metadata

_DEFAULT_POSIX_TMP_ROOT = Path("/tmp") if os.name == "posix" else None


def _resolve_path(path: Path) -> Path | None:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _resolve_candidate(path: str, home_root: Path) -> Path | None:
    value = path.strip()
    if not value:
        return None

    if value == "~":
        candidate = home_root
    elif value.startswith("~/") or (os.sep == "\\" and value.startswith("~\\")):
        candidate = home_root / value[2:]
    elif value.startswith("~"):
        return None
    else:
        candidate = Path(value)
        if not candidate.is_absolute():
            return None

    return _resolve_path(candidate)


def _provider_user_directory(provider: str | None) -> str | None:
    if not isinstance(provider, str):
        return None
    normalized = provider.strip().lower()
    if not normalized:
        return None
    return next(
        (
            metadata.user_directory
            for metadata in provider_metadata()
            if metadata.provider == normalized
        ),
        None,
    )


def is_plan_scratch_path(
    path: str,
    provider: str | None,
    *,
    home_root: str | Path | None = None,
    temp_root: str | Path | None = None,
    posix_tmp_root: str | Path | None = _DEFAULT_POSIX_TMP_ROOT,
) -> bool:
    """Return whether *path* is an approved plan-mode scratch location.

    Accepted locations are the active provider's canonical user directory,
    the OS temporary directory, and resolved ``/tmp`` on POSIX. Relative
    project paths and paths that escape an approved root through symlinks or
    traversal fail closed.
    """
    user_directory = _provider_user_directory(provider)
    if not isinstance(path, str) or user_directory is None:
        return False

    resolved_home = _resolve_path(Path.home() if home_root is None else Path(home_root))
    if resolved_home is None:
        return False
    candidate = _resolve_candidate(path, resolved_home)
    if candidate is None:
        return False

    roots: list[Path] = []
    provider_root = _resolve_path(resolved_home / user_directory)
    if provider_root is not None and provider_root.is_relative_to(resolved_home):
        roots.append(provider_root)

    configured_temp = Path(tempfile.gettempdir()) if temp_root is None else Path(temp_root)
    resolved_temp = _resolve_path(configured_temp)
    if resolved_temp is not None:
        roots.append(resolved_temp)

    if posix_tmp_root is not None:
        resolved_posix_tmp = _resolve_path(Path(posix_tmp_root))
        if resolved_posix_tmp is not None:
            roots.append(resolved_posix_tmp)

    return any(candidate == root or candidate.is_relative_to(root) for root in roots)


def is_project_plan_artifact_path(
    path: str,
    project_root: str | Path | None,
) -> bool:
    """Return whether *path* is a Markdown plan beneath ``.gobby/plans``."""
    if not isinstance(path, str) or not path.strip() or project_root is None:
        return False

    resolved_project = _resolve_path(Path(project_root).expanduser())
    if resolved_project is None:
        return False

    candidate = Path(path.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = resolved_project / candidate
    resolved_candidate = _resolve_path(candidate)
    if resolved_candidate is None:
        return False

    try:
        relative = resolved_candidate.relative_to(resolved_project)
    except ValueError:
        return False

    return (
        len(relative.parts) >= 3
        and relative.parts[:2] == (".gobby", "plans")
        and relative.suffix == ".md"
    )
