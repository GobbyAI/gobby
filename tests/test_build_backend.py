"""Tests for the PEP 517 build backend wrapper that stages the web UI."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _load_backend(repo_root: Path) -> object:
    """Import build_backend rooted at ``repo_root``."""
    sys.path.insert(0, str(repo_root))
    try:
        if "build_backend" in sys.modules:
            del sys.modules["build_backend"]
        return importlib.import_module("build_backend")
    finally:
        sys.path.pop(0)


def test_stage_ui_copies_dist_to_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    # Symlink __init__.py from the real module so we exercise the actual code.
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())

    web = repo_root / "web"
    dist = web / "dist"
    dist.mkdir(parents=True)
    (web / "package.json").write_text("{}")
    (dist / "index.html").write_text("<html></html>")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("// js")

    # Skip the npm step; we only want to test the copy phase.
    monkeypatch.setenv("GOBBY_SKIP_UI_BUILD", "0")
    # Force no-npm path so npm ci is never invoked.
    monkeypatch.setattr("shutil.which", lambda name: None if name == "npm" else "/usr/bin/" + name)

    backend = _load_backend(repo_root)
    backend._stage_ui()  # type: ignore[attr-defined]

    staged = repo_root / "src" / "gobby" / "ui" / "web" / "dist"
    assert (staged / "index.html").read_text() == "<html></html>"
    assert (staged / "assets" / "app.js").read_text() == "// js"


def test_stage_ui_skip_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())

    # Create a stale dist; staging must NOT touch it.
    web_dist = repo_root / "web" / "dist"
    web_dist.mkdir(parents=True)
    (web_dist / "index.html").write_text("fresh")
    staged_dir = repo_root / "src" / "gobby" / "ui" / "web" / "dist"
    staged_dir.mkdir(parents=True)
    (staged_dir / "index.html").write_text("stale")

    monkeypatch.setenv("GOBBY_SKIP_UI_BUILD", "1")

    backend = _load_backend(repo_root)
    backend._stage_ui()  # type: ignore[attr-defined]

    assert (staged_dir / "index.html").read_text() == "stale"


def test_stage_ui_reuses_pre_staged_when_no_web(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())

    # No web/ tree at all; only a pre-staged dist exists (sdist install scenario).
    staged_dir = repo_root / "src" / "gobby" / "ui" / "web" / "dist"
    staged_dir.mkdir(parents=True)
    (staged_dir / "index.html").write_text("pre-staged")

    monkeypatch.delenv("GOBBY_SKIP_UI_BUILD", raising=False)

    backend = _load_backend(repo_root)
    backend._stage_ui()  # type: ignore[attr-defined]

    assert (staged_dir / "index.html").read_text() == "pre-staged"
