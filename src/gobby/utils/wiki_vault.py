"""Shared wiki vault layout and directory resolution.

Python mirror of ``gobby_core::vault`` (``crates/gcore/src/vault.rs``). The
daemon, gwiki, and gcode must agree on these names and on the resolution
order so every surface addresses the same on-disk vault. Change the Rust
module and this one together.
"""

from __future__ import annotations

from pathlib import Path

STATE_ROOT = "_gwiki"
SCOPE_FILE = "scope.json"
DEFAULT_VAULT_DIR = "wiki"
FALLBACK_VAULT_DIR = "gobby-wiki"

_MAX_NUMBERED_FALLBACKS = 999


def is_vault(directory: Path) -> bool:
    """Return whether ``directory`` is an initialized wiki vault."""
    return (directory / STATE_ROOT / SCOPE_FILE).is_file()


def resolve_vault_dir(project_root: Path) -> Path | None:
    """Resolve the wiki vault directory for ``project_root``.

    Prefers ``wiki/`` when it is a vault or free, then ``gobby-wiki/``, then
    ``gobby-wiki-001/`` .. ``gobby-wiki-999/``. Returns ``None`` only when
    every candidate is occupied by a non-vault path.
    """
    preferred = project_root / DEFAULT_VAULT_DIR
    if _is_vault_or_free(preferred):
        return preferred
    fallback = project_root / FALLBACK_VAULT_DIR
    if _is_vault_or_free(fallback):
        return fallback
    for attempt in range(1, _MAX_NUMBERED_FALLBACKS + 1):
        candidate = project_root / f"{FALLBACK_VAULT_DIR}-{attempt:03d}"
        if _is_vault_or_free(candidate):
            return candidate
    return None


def existing_vault_dir(project_root: Path) -> Path | None:
    """Return the resolved vault directory only when it is already initialized.

    Read paths use this: a resolved-but-free slot has nothing to read, and a
    resolved directory that exists is a vault by construction.
    """
    resolved = resolve_vault_dir(project_root)
    if resolved is not None and is_vault(resolved):
        return resolved
    return None


def _is_vault_or_free(directory: Path) -> bool:
    return not directory.exists() or is_vault(directory)
