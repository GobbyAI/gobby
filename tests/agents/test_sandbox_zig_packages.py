"""Spawned sandbox runs get writable, fetch-free Zig caches for libghostty-vt."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from gobby.agents import sandbox_policy
from gobby.agents.zig_packages import ZIG_PACKAGES, materialize_zig_packages

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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workspace: Path, run_id: str = "run-1"
) -> tuple[Path, dict[str, str]]:
    # The Zig mirror outlives runs, so it must not outlive the test.
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    paths = sandbox_policy.prepare_sandbox_run_paths(run_id, {}, workspace=workspace)
    return paths.cache, paths.environment("claude")


def test_worktree_runs_share_one_stable_zig_system_dir_of_main_checkout_packages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gterminal's build script reruns whenever LIBGHOSTTY_VT_ZIG_SYSTEM_DIR changes,
    so a per-run value rebuilds libghostty-vt on every sandboxed run (#23194)."""
    main, worktree = _linked_worktree(tmp_path)
    main_zig_pkg = _vendored(main) / "zig-pkg"
    _extracted_package(main_zig_pkg, "main-checkout-payload")
    _vendored(worktree)
    _machine_cache(monkeypatch, tmp_path)

    cache, env = _run_environment(monkeypatch, tmp_path, worktree)
    _second_cache, second_env = _run_environment(monkeypatch, tmp_path, worktree, "run-2")

    zig_cache = Path(env["ZIG_GLOBAL_CACHE_DIR"])
    assert zig_cache.is_relative_to(cache)
    assert zig_cache.is_dir()
    system_dir = Path(env["LIBGHOSTTY_VT_ZIG_SYSTEM_DIR"])
    assert second_env["LIBGHOSTTY_VT_ZIG_SYSTEM_DIR"] == str(system_dir)
    assert not system_dir.is_relative_to(cache)
    package = system_dir / PKGID
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


def test_stable_mirror_relinks_a_package_whose_source_vanished(tmp_path: Path) -> None:
    """The mirror outlives runs, so a package linked from a since-cleaned checkout
    must be relinked rather than served to Zig as dangling links."""
    machine_pkgs = tmp_path / "machine"
    machine_pkgs.mkdir()
    (machine_pkgs / f"{PKGID}.tar.gz").write_bytes(b"not read: a vendored copy is reused")
    first_zig_pkg = tmp_path / "first" / "zig-pkg"
    second_zig_pkg = tmp_path / "second" / "zig-pkg"
    _extracted_package(first_zig_pkg, "first-payload")
    _extracted_package(second_zig_pkg, "second-payload")
    mirror = tmp_path / "mirror"
    reports: list[str] = []

    assert materialize_zig_packages(
        machine_pkgs, mirror, first_zig_pkg, manifest=None, report=reports.append
    )
    shutil.rmtree(first_zig_pkg)
    assert materialize_zig_packages(
        machine_pkgs, mirror, second_zig_pkg, manifest=None, report=reports.append
    )

    package = mirror / ZIG_PACKAGES / PKGID
    assert (package / "src").readlink() == second_zig_pkg / PKGID / "src"
    assert (package / "src" / "marker.txt").read_text(encoding="utf-8") == "second-payload"
    assert [entry.name for entry in mirror.iterdir()] == [ZIG_PACKAGES]
    assert reports == []


MISSING = "vaxis-0.6.0-BWNVXXXX"


def test_every_pin_present_in_the_machine_cache_completes_the_mirror(tmp_path: Path) -> None:
    machine_pkgs = tmp_path / "machine"
    machine_pkgs.mkdir()
    _extracted_package(machine_pkgs, "machine-payload")
    manifest = tmp_path / "build.zig.zon"
    manifest.write_text(
        f'.{{ .dependencies = .{{ .dep = .{{ .hash = "{PKGID}" }} }} }}\n', encoding="utf-8"
    )
    reports: list[str] = []

    assert materialize_zig_packages(
        machine_pkgs, tmp_path / "mirror", None, manifest=manifest, report=reports.append
    )
    assert reports == []


def test_a_lazy_pin_missing_from_the_machine_cache_still_completes_the_mirror(
    tmp_path: Path,
) -> None:
    """A --system build resolves a lazy dependency only when build.zig asks
    for it, so its absence alone does not fail the mirror."""
    machine_pkgs = tmp_path / "machine"
    machine_pkgs.mkdir()
    _extracted_package(machine_pkgs, "machine-payload")
    lazy_pkg = tmp_path / "pkg" / "lazy"
    lazy_pkg.mkdir(parents=True)
    (lazy_pkg / "build.zig.zon").write_text(
        f'.{{ .dependencies = .{{ .tarball = .{{ .hash = "{MISSING}" }} }} }}\n',
        encoding="utf-8",
    )
    manifest = tmp_path / "build.zig.zon"
    manifest.write_text(
        ".{ .dependencies = .{ "
        f'.dep = .{{ .hash = "{PKGID}" }}, '
        f'.lazy_pin = .{{ .hash = "{MISSING}", .lazy = true }}, '
        '.lazy_path = .{ .path = "./pkg/lazy", .lazy = true }, '
        "} }\n",
        encoding="utf-8",
    )
    reports: list[str] = []

    assert materialize_zig_packages(
        machine_pkgs, tmp_path / "mirror", None, manifest=manifest, report=reports.append
    )
    assert reports == []
    assert (tmp_path / "mirror" / ZIG_PACKAGES / PKGID).is_dir()


@pytest.mark.parametrize("pinned_by", ["vendored", "package"])
def test_a_pin_missing_from_the_machine_cache_names_the_package_and_the_refresh(
    tmp_path: Path, pinned_by: str
) -> None:
    """A dependency bump the machine cache lacks fails here, naming its fix,
    instead of inside a fetch-disabled sandboxed build."""
    machine_pkgs = tmp_path / "machine"
    machine_pkgs.mkdir()
    package = _extracted_package(machine_pkgs, "machine-payload")
    vendored = tmp_path / "vendored"
    vendored.mkdir()
    manifest = vendored / "build.zig.zon"
    pins = [PKGID] if pinned_by == "package" else [PKGID, MISSING]
    manifest.write_text(
        ".{ .dependencies = .{ "
        + " ".join(f'.dep{index} = .{{ .hash = "{pin}" }},' for index, pin in enumerate(pins))
        + " } }\n",
        encoding="utf-8",
    )
    if pinned_by == "package":
        (package / "build.zig.zon").write_text(
            f'.{{ .dependencies = .{{ .nested = .{{ .hash = "{MISSING}" }} }} }}\n',
            encoding="utf-8",
        )
    reports: list[str] = []

    complete = materialize_zig_packages(
        machine_pkgs, tmp_path / "mirror", None, manifest=manifest, report=reports.append
    )

    assert not complete
    assert reports == [
        f"zig package {MISSING} pinned by the vendored libghostty-vt is missing from "
        f"{machine_pkgs}; refresh it with scripts/build_vendored_libghostty_vt.sh"
    ]
