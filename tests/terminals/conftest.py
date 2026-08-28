"""Shared fixtures for the cross-backend runtime contract suite (plan 5.1)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


def gterm_binary() -> Path | None:
    """Return the first gterm binary on the isolated native-bin search path."""
    env = os.environ.get("GOBBY_NATIVE_BIN_DIR")
    if env:
        candidate = Path(env) / "gterm"
        if candidate.is_file():
            return candidate
    worktree = Path(__file__).resolve().parents[2]
    for directory in (
        worktree / "target" / "debug",
        worktree / ".gobby-native-bin",
        Path.home() / ".gobby" / "bin",
    ):
        candidate = directory / "gterm"
        if candidate.is_file():
            return candidate
    which = shutil.which("gterm")
    return Path(which) if which else None


def require_backend(backend: str) -> None:
    """Skip a contract cell when its real backend binary is absent."""
    if backend == "tmux" and shutil.which("tmux") is None:
        pytest.skip("tmux binary is not available")
    if backend == "native" and gterm_binary() is None:
        pytest.skip("gterm binary is not available")


@pytest.fixture(params=["tmux", "native"])
def contract_backend(request: pytest.FixtureRequest) -> str:
    backend = str(request.param)
    require_backend(backend)
    return backend
