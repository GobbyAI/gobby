"""Compatibility migrations for wiki configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_LEGACY_WIKI_SUFFIX = (".gobby", "wiki")
_PROJECT_WIKI_DIR = "gobby-wiki"


def migrate_legacy_wiki_roots(config_dict: dict[str, Any]) -> None:
    """Rewrite legacy project wiki roots to the top-level project vault."""
    wiki = config_dict.get("wiki")
    if not isinstance(wiki, dict):
        return

    roots = wiki.get("roots")
    if not isinstance(roots, list):
        return

    for root in roots:
        if isinstance(root, dict) and "path" in root:
            root["path"] = _migrate_legacy_wiki_path(root["path"])


def _migrate_legacy_wiki_path(value: Any) -> Any:
    if isinstance(value, Path):
        migrated = _legacy_project_wiki_path(value)
        return migrated if migrated is not None else value
    if isinstance(value, str):
        migrated = _legacy_project_wiki_path(Path(value))
        return str(migrated) if migrated is not None else value
    return value


def _legacy_project_wiki_path(path: Path) -> Path | None:
    if len(path.parts) >= 2 and path.parts[-2:] == _LEGACY_WIKI_SUFFIX:
        return path.parent.parent / _PROJECT_WIKI_DIR
    return None
