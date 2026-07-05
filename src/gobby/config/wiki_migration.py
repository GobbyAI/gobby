"""Compatibility migrations for wiki configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gobby.utils.wiki_vault import DEFAULT_VAULT_DIR, FALLBACK_VAULT_DIR


def migrate_legacy_wiki_roots(config_dict: dict[str, Any]) -> None:
    """Rewrite ``gobby-wiki`` project wiki roots to the sibling ``wiki`` vault."""
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
    if path.name == FALLBACK_VAULT_DIR:
        return path.parent / DEFAULT_VAULT_DIR
    return None
