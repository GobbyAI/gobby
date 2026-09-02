from __future__ import annotations

import stat
from pathlib import Path

import pytest

from gobby.agents import sandbox_policy

pytestmark = pytest.mark.unit


def _workspace(tmp_path: Path, *, configured: bool = True) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if configured:
        (workspace / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    return workspace


def _operator_store(path: Path, marker: str) -> Path:
    repo = path / "repoabc"
    repo.mkdir(parents=True)
    (path / "db.db").write_text(f"database:{marker}\n", encoding="utf-8")
    (repo / "hook.py").write_text(f"hook:{marker}\n", encoding="utf-8")
    return path


def _run_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    workspace: Path,
) -> tuple[sandbox_policy.SandboxRunPaths, Path]:
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setattr(sandbox_policy, "get_gobby_home", lambda: gobby_home)
    paths = sandbox_policy.prepare_sandbox_run_paths(
        "run-1",
        {},
        workspace=workspace,
    )
    destination = Path(paths.environment("codex")["XDG_CACHE_HOME"]) / "pre-commit"
    return paths, destination


def test_prepare_sandbox_run_paths_copies_writable_isolated_pre_commit_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = _operator_store(tmp_path / "operator-pre-commit", "selected")
    _operator_store(tmp_path / "xdg" / "pre-commit", "decoy")
    monkeypatch.setenv("PRE_COMMIT_HOME", str(source))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    (source / "db.db").chmod(0o400)
    (source / "repoabc" / "hook.py").chmod(0o400)
    (source / "repoabc").chmod(0o500)
    source.chmod(0o500)
    source_modes = {
        path.relative_to(source): stat.S_IMODE(path.stat().st_mode)
        for path in (source, *source.rglob("*"))
    }

    paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    environment = paths.environment("codex")
    assert environment["XDG_CACHE_HOME"] == str(paths.cache / "xdg-cache-home")
    assert "PRE_COMMIT_HOME" not in environment
    assert (destination / "db.db").read_text(encoding="utf-8") == "database:selected\n"
    assert (destination / "repoabc" / "hook.py").read_text(encoding="utf-8") == ("hook:selected\n")
    assert all(
        path.stat().st_mode & stat.S_IWUSR for path in (destination, *destination.rglob("*"))
    )

    (destination / "db.db").write_text("updated copy\n", encoding="utf-8")
    (destination / ".lock").write_text("", encoding="utf-8")
    (destination / "repoabc" / "generated").write_text("run-owned\n", encoding="utf-8")
    assert (source / "db.db").read_text(encoding="utf-8") == "database:selected\n"
    assert not (source / ".lock").exists()
    assert not (source / "repoabc" / "generated").exists()
    assert source_modes == {
        path.relative_to(source): stat.S_IMODE(path.stat().st_mode)
        for path in (source, *source.rglob("*"))
    }


def test_prepare_sandbox_run_paths_uses_xdg_pre_commit_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = _operator_store(tmp_path / "xdg" / "pre-commit", "xdg")
    monkeypatch.delenv("PRE_COMMIT_HOME", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(source.parent))

    _paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    assert (destination / "db.db").read_text(encoding="utf-8") == "database:xdg\n"


def test_prepare_sandbox_run_paths_uses_default_pre_commit_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    home = tmp_path / "home"
    _operator_store(home / ".cache" / "pre-commit", "default")
    monkeypatch.delenv("PRE_COMMIT_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))

    _paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    assert (destination / "db.db").read_text(encoding="utf-8") == "database:default\n"


def test_prepare_sandbox_run_paths_skips_pre_commit_store_without_workspace_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, configured=False)
    source = _operator_store(tmp_path / "operator-pre-commit", "operator")
    monkeypatch.setenv("PRE_COMMIT_HOME", str(source))

    _paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    assert not destination.exists()


def test_prepare_sandbox_run_paths_skips_missing_operator_pre_commit_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setenv("PRE_COMMIT_HOME", str(tmp_path / "missing"))

    _paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    assert not destination.exists()
