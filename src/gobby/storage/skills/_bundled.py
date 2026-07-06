"""Detection of bundled skill-template source paths.

Bundled skill templates live under ``gobby/install/shared/skills/`` — repo
checkouts and git worktrees add a ``src/`` prefix, installed packages nest the
same tree under ``site-packages``. Rows sourced from these paths are only
valid as bundled-synced installed rows: a project-scoped row pointing at a
template shadows the installed row with stale template content (#17606).
"""

from __future__ import annotations

from pathlib import PurePath

__all__ = ["BUNDLED_TEMPLATE_PROJECT_SKILL_ERROR", "is_bundled_template_path"]

_BUNDLED_SKILLS_COMPONENTS = ("gobby", "install", "shared", "skills")

BUNDLED_TEMPLATE_PROJECT_SKILL_ERROR = (
    "bundled skill templates under gobby/install/shared/skills/ are synced "
    "automatically as installed skills and cannot be registered as "
    "project-scoped skills"
)


def is_bundled_template_path(source_path: str | None) -> bool:
    """True when ``source_path`` points inside a bundled skill-template tree.

    Matches any path whose components contain the contiguous sequence
    ``gobby/install/shared/skills`` — repo checkouts, git worktrees, and
    site-packages installs alike.
    """
    if not source_path:
        return False
    parts = PurePath(source_path).parts
    window = len(_BUNDLED_SKILLS_COMPONENTS)
    return any(
        parts[i : i + window] == _BUNDLED_SKILLS_COMPONENTS for i in range(len(parts) - window + 1)
    )
