"""Spawned sandbox runs get writable, fetch-free Zig caches for libghostty-vt."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from gobby.agents import sandbox_policy

pytestmark = pytest.mark.unit

PKGID = "libxev-0.0.0-86vtcXXXX"


@pytest.fixture(autouse=True)
def _cleanup_pre_commit_store_spare() -> Iterator[None]:
    yield
    sandbox_policy.shutdown_pre_commit_store_spare()


def _vendored(checkout: Path) -> Path:
    vendored = checkout / "crates" / "gterminal" / "vendor" / "libghostty-vt"
    vendored.mkdir(parents=True)
    return vendored


def _extracted_package(parent: Path, marker: str) -> Path:
    package = parent / PKGID
    (package / "src").mkdir(parents=True)
    (package / "src" / "marker.txt").write_text(marker, encoding="utf-8")
    return package


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / "main"
    git_dir = main / ".git" / "worktrees" / "wt"
    git_dir.mkdir(parents=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    return main, worktree


def _machine_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, extra_entry: bool = False
) -> Path:
    home = tmp_path / "home"
    machine_pkgs = home / ".cache" / "zig" / "p"
    machine_pkgs.mkdir(parents=True)
    (machine_pkgs / f"{PKGID}.tar.gz").write_bytes(b"not read: the vendored copy is reused")
    if extra_entry:
        (machine_pkgs / "mystery.entry").write_text("not a zig package\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return machine_pkgs


def _run_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workspace: Path
) -> tuple[Path, dict[str, str]]:
    monkeypatch.setattr(sandbox_policy, "get_gobby_home", lambda: tmp_path / "gobby-home")
    paths = sandbox_policy.prepare_sandbox_run_paths("run-1", {}, workspace=workspace)
    return paths.cache, paths.environment("claude")


def test_worktree_run_links_main_checkout_packages_into_run_zig_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    main, worktree = _linked_worktree(tmp_path)
    main_zig_pkg = _vendored(main) / "zig-pkg"
    _extracted_package(main_zig_pkg, "main-checkout-payload")
    _vendored(worktree)
    _machine_cache(monkeypatch, tmp_path)

    cache, env = _run_environment(monkeypatch, tmp_path, worktree)

    zig_cache = Path(env["ZIG_GLOBAL_CACHE_DIR"])
    assert zig_cache.is_relative_to(cache)
    assert zig_cache.is_dir()
    assert env["LIBGHOSTTY_VT_ZIG_SYSTEM_DIR"] == str(zig_cache / "p")
    package = zig_cache / "p" / PKGID
    assert not package.is_symlink()
    assert (package / "src").readlink() == main_zig_pkg / PKGID / "src"
    assert (package / "src" / "marker.txt").read_text(encoding="utf-8") == ("main-checkout-payload")


def test_run_without_vendored_libghostty_vt_gets_only_writable_zig_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _machine_cache(monkeypatch, tmp_path)

    cache, env = _run_environment(monkeypatch, tmp_path, workspace)

    assert Path(env["ZIG_GLOBAL_CACHE_DIR"]).is_relative_to(cache)
    assert "LIBGHOSTTY_VT_ZIG_SYSTEM_DIR" not in env
    assert not (Path(env["ZIG_GLOBAL_CACHE_DIR"]) / "p").exists()


def test_incomplete_package_dir_is_not_exported_as_system_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    _extracted_package(_vendored(workspace) / "zig-pkg", "workspace-payload")
    _machine_cache(monkeypatch, tmp_path, extra_entry=True)

    cache, env = _run_environment(monkeypatch, tmp_path, workspace)

    assert Path(env["ZIG_GLOBAL_CACHE_DIR"]).is_relative_to(cache)
    assert "LIBGHOSTTY_VT_ZIG_SYSTEM_DIR" not in env
