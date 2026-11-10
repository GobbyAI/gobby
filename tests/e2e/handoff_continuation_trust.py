"""Read-only provider trust for the handoff proof's fixed project folder (#25771).

The real seat logs in with the operator's HOME, and that HOME's provider config also holds
workspace trust. The proof must not write that config (security review 49c502c1), so it
runs in one fixed folder under ~/.gobby that the operator already trusts (Josh, 2026-11-09:
"use a ~/.gobby/ folder") and refuses fast, naming the one-time step, when it is not.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from tests.e2e.composer_proof import ProofRefused
from tests.e2e.conftest import build_e2e_project, keep_e2e_artifacts


def project_folder(home: Path) -> Path:
    return home / ".gobby" / "e2e" / "handoff-continuation-project"


def _self_and_ancestors(folder: Path) -> list[str]:
    return [str(path) for path in (folder, *folder.parents)]


def claude_trusts(config: Mapping[str, object], folder: Path) -> bool:
    """True when the folder or an ancestor has an accepted Claude trust dialog."""
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return False
    for key in _self_and_ancestors(folder):
        entry = projects.get(key)
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return True
    return False


def codex_trusts(config: Mapping[str, object], folder: Path) -> bool:
    """True when the folder or an ancestor is a trusted Codex project."""
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return False
    for key in _self_and_ancestors(folder):
        entry = projects.get(key)
        if isinstance(entry, dict) and entry.get("trust_level") == "trusted":
            return True
    return False


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
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(mode=0o700)
    try:
        build_e2e_project(folder)
        yield folder
    finally:
        if not keep_e2e_artifacts():
            shutil.rmtree(folder, ignore_errors=True)
