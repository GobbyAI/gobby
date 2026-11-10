"""Read-only provider trust for the handoff proof's fixed project folder (#25771).

The real seat logs in with the operator's HOME, and that HOME's provider config also holds
workspace trust. The proof must not write that config (security review 49c502c1), so it
runs in one fixed folder under ~/.gobby that the operator already trusts (Josh, 2026-11-09:
"use a ~/.gobby/ folder") and refuses fast, naming the one-time step, when it is not.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tomllib
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from tests.e2e.composer_proof import ProofRefused
from tests.e2e.conftest import build_e2e_project, keep_e2e_artifacts


def project_folder(home: Path) -> Path:
    return home / ".gobby" / "e2e" / "handoff-continuation-project"


def _entries(config: Mapping[str, object], folder: Path) -> list[Mapping[str, object]]:
    # The folder is a plain directory, not a git repo: Claude bounds its trust walk at an
    # enclosing repo root (observed with Claude 2.1.322, where a repo-root folder sat at the
    # trust prompt despite a trusted ~/.gobby). Outside any repo the walk reaches every
    # ancestor, so ~/.gobby's existing trust covers the folder.
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return []
    found = [projects.get(str(path)) for path in (folder, *folder.parents)]
    return [entry for entry in found if isinstance(entry, dict)]


def claude_trusts(config: Mapping[str, object], folder: Path) -> bool:
    """True when the folder or an ancestor has an accepted Claude trust dialog."""
    return any(entry.get("hasTrustDialogAccepted") is True for entry in _entries(config, folder))


def codex_trusts(config: Mapping[str, object], folder: Path) -> bool:
    """True when the folder or an ancestor is a trusted Codex project."""
    return any(entry.get("trust_level") == "trusted" for entry in _entries(config, folder))


def enclosing_git_root(folder: Path) -> Path | None:
    """The nearest folder or ancestor holding ``.git``; it would bound the trust walk."""
    for path in (folder, *folder.parents):
        if (path / ".git").exists():
            return path
    return None


def _read_claude(home: Path) -> Mapping[str, object]:
    try:
        loaded = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _read_codex(home: Path) -> Mapping[str, object]:
    try:
        return tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def require_provider_trust(provider: str, folder: Path, *, home: Path) -> None:
    """Read the operator's trust state; refuse with the exact one-time step when untrusted."""
    repo = enclosing_git_root(folder)
    if repo is not None:
        raise ProofRefused(
            f"{provider}: {folder} is inside git repo {repo}; the CLI would stop its trust "
            "lookup there, so the proof folder must stay a plain directory"
        )
    if provider == "claude":
        trusted = claude_trusts(_read_claude(home), folder)
        step = f"run `claude` once in {folder} and accept the folder-trust dialog"
    elif provider == "codex":
        trusted = codex_trusts(_read_codex(home), folder)
        step = f"run `codex` once in {folder} and choose to trust the folder"
    else:
        raise ProofRefused(
            f"{provider}: no read-only trust check exists; the proof will not write provider "
            f"trust, so pre-trust {folder} by hand and add a reader for this provider"
        )
    if not trusted:
        raise ProofRefused(
            f"{provider}: {folder} is not trusted and the proof never writes provider trust; "
            f"one-time operator step: {step}"
        )


@pytest.fixture
def e2e_project_dir() -> Iterator[Path]:
    """The fixed, operator-trusted project folder, rebuilt fresh for every case."""
    folder = project_folder(Path.home())
    if folder.is_symlink() or folder.parent.is_symlink():
        raise ProofRefused(f"{folder} must be a real directory, not a symlink")
    folder.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    remove_project_folder(folder)
    folder.mkdir(mode=0o700)
    try:
        # A plain directory: the handoff cases never touch clones or worktrees.
        build_e2e_project(folder, git=False)
        yield folder
    finally:
        if not keep_e2e_artifacts():
            remove_project_folder(folder)


def _restore_write_and_retry(
    function: Callable[..., object], path: str, _exc: BaseException
) -> None:
    # The isolated daemon installs its sandbox runtime read-only under its HOME, which lives
    # inside this folder; restore the owner write bit on the parent (never following a link)
    # and retry, the same recovery TemporaryDirectory applies.
    parent = os.path.dirname(path)
    os.chmod(parent, stat.S_IRWXU, follow_symlinks=False)
    if os.path.isdir(path) and not os.path.islink(path):
        os.chmod(path, stat.S_IRWXU, follow_symlinks=False)
    function(path)


def remove_project_folder(folder: Path) -> None:
    """Remove the proof folder, read-only runtime trees included, and prove it is gone."""
    if not folder.exists() and not folder.is_symlink():
        return
    shutil.rmtree(folder, onexc=_restore_write_and_retry)
    if folder.exists() or folder.is_symlink():
        raise ProofRefused(f"{folder} could not be removed; clear it before the next proof")
