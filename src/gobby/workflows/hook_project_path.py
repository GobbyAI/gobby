"""Project-path resolution for workflow hook rules (#24524 RED stub)."""

from __future__ import annotations

_NO_REPO_SYSTEM_PROJECTS: frozenset[str] = frozenset()


def _is_known_no_repo_project(project_id: str | None) -> bool:
    raise NotImplementedError
