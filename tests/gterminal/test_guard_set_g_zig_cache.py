"""Guard set G sandboxed Zig package cache and TMPDIR preparation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from gobby.guard_set_g import _isolated_child_env, _materialize_zig_packages

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GTERMINAL = REPO_ROOT / "crates" / "gterminal"
LIBSYSTEM_OVERRIDE = (
    GTERMINAL / "vendor" / "libghostty-vt" / "src" / "build" / "libsystem_override.sh"
)


def _make_extracted_package(parent: Path, pkgid: str, marker: str) -> Path:
    package = parent / pkgid
    (package / "src").mkdir(parents=True)
    (package / "build.zig.zon").write_text(".{ .name = .pkg }\n", encoding="utf-8")
    (package / "src" / "marker.txt").write_text(marker, encoding="utf-8")
    return package


def _make_tarball_package(parent: Path, pkgid: str, marker: str) -> Path:
    wrapper = parent / f"{pkgid}.staging"
    staging = _make_extracted_package(wrapper, pkgid, marker)
    tarball = parent / f"{pkgid}.tar.gz"
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(staging, arcname=pkgid)
    shutil.rmtree(wrapper)
    return tarball


def test_materialize_extracts_tarball_only_package(tmp_path: Path) -> None:
    source = tmp_path / "p"
    source.mkdir()
    _make_tarball_package(source, "libxev-0.0.0-86vtcXXXX", "tarball-payload")
    cache_root = tmp_path / "zig-cache"

    assert _materialize_zig_packages(source, cache_root, None)

    packages = cache_root / "p"
    extracted = packages / "libxev-0.0.0-86vtcXXXX"
    assert not extracted.is_symlink()
    assert (extracted / "src" / "marker.txt").read_text(encoding="utf-8") == "tarball-payload"
    assert list(packages.iterdir()) == [extracted]
    assert _materialize_zig_packages(source, cache_root, None)


def test_materialize_links_already_extracted_package(tmp_path: Path) -> None:
    source = tmp_path / "p"
    source.mkdir()
    extracted = _make_extracted_package(source, "uucode-0.2.0-ZZjBPXXXX", "extracted-payload")
    cache_root = tmp_path / "zig-cache"

    assert _materialize_zig_packages(source, cache_root, None)

    package = cache_root / "p" / "uucode-0.2.0-ZZjBPXXXX"
    assert package.is_symlink(), "an already-extracted package is linked, never copied"
    assert package.readlink() == extracted
    assert (package / "src" / "marker.txt").read_text(encoding="utf-8") == "extracted-payload"
    assert extracted.is_dir()


def test_materialize_reuses_vendored_extracted_copy_when_hashes_match(tmp_path: Path) -> None:
    source = tmp_path / "p"
    source.mkdir()
    _make_tarball_package(source, "vaxis-0.6.0-BWNVXXXX", "tarball-payload")
    vendored_zig_pkg = tmp_path / "zig-pkg"
    _make_extracted_package(vendored_zig_pkg, "vaxis-0.6.0-BWNVXXXX", "vendored-payload")
    cache_root = tmp_path / "zig-cache"

    assert _materialize_zig_packages(source, cache_root, vendored_zig_pkg)

    package = cache_root / "p" / "vaxis-0.6.0-BWNVXXXX"
    assert package.readlink() == vendored_zig_pkg / "vaxis-0.6.0-BWNVXXXX"
    assert (package / "src" / "marker.txt").read_text(encoding="utf-8") == "vendored-payload"


def test_materialize_reports_incomplete_for_unrecognized_entry(tmp_path: Path) -> None:
    source = tmp_path / "p"
    source.mkdir()
    _make_extracted_package(source, "uucode-0.2.0-ZZjBPXXXX", "extracted-payload")
    (source / "mystery.entry").write_text("not a zig package\n", encoding="utf-8")
    cache_root = tmp_path / "zig-cache"

    assert not _materialize_zig_packages(source, cache_root, None)

    marker = cache_root / "p" / "uucode-0.2.0-ZZjBPXXXX" / "src" / "marker.txt"
    assert marker.read_text(encoding="utf-8") == "extracted-payload"
    assert not (cache_root / "p" / "mystery.entry").exists()


def test_isolated_child_env_materializes_run_scoped_zig_package_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    machine_pkgs = home / ".cache" / "zig" / "p"
    machine_pkgs.mkdir(parents=True)
    _make_extracted_package(machine_pkgs, "uucode-0.2.0-ZZjBPXXXX", "extracted-payload")
    _make_tarball_package(machine_pkgs, "libxev-0.0.0-86vtcXXXX", "tarball-payload")
    vendored_zig_pkg = (
        tmp_path / "repo" / "crates" / "gterminal" / "vendor" / "libghostty-vt" / "zig-pkg"
    )
    vendored_zig_pkg.mkdir(parents=True)
    _make_extracted_package(vendored_zig_pkg, "libxev-0.0.0-86vtcXXXX", "vendored-payload")
    monkeypatch.setenv("HOME", str(home))
    run_root = tmp_path / "run"
    run_root.mkdir()

    env = _isolated_child_env(run_root, {"PATH": "/bin"}, repo=tmp_path / "repo")

    cache_root = run_root / "zig-cache"
    assert env["ZIG_GLOBAL_CACHE_DIR"] == str(cache_root)
    assert env["LIBGHOSTTY_VT_ZIG_SYSTEM_DIR"] == str(cache_root / "p")
    packages = cache_root / "p"
    extracted = packages / "uucode-0.2.0-ZZjBPXXXX"
    assert extracted.readlink() == machine_pkgs / "uucode-0.2.0-ZZjBPXXXX"
    assert (extracted / "src" / "marker.txt").read_text(encoding="utf-8") == "extracted-payload"
    reused = packages / "libxev-0.0.0-86vtcXXXX"
    assert reused.readlink() == vendored_zig_pkg / "libxev-0.0.0-86vtcXXXX"
    assert (reused / "src" / "marker.txt").read_text(encoding="utf-8") == "vendored-payload"
    assert sorted(entry.name for entry in packages.iterdir()) == [
        "libxev-0.0.0-86vtcXXXX",
        "uucode-0.2.0-ZZjBPXXXX",
    ]


def test_isolated_child_env_leaves_system_dir_unset_when_materialization_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    machine_pkgs = home / ".cache" / "zig" / "p"
    machine_pkgs.mkdir(parents=True)
    _make_extracted_package(machine_pkgs, "uucode-0.2.0-ZZjBPXXXX", "extracted-payload")
    (machine_pkgs / "mystery.entry").write_text("not a zig package\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    run_root = tmp_path / "run"
    run_root.mkdir()

    env = _isolated_child_env(run_root, {"PATH": "/bin"})

    assert env["ZIG_GLOBAL_CACHE_DIR"] == str(run_root / "zig-cache")
    assert "LIBGHOSTTY_VT_ZIG_SYSTEM_DIR" not in env


def test_isolated_child_env_omits_system_dir_when_machine_cache_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / ".cache" / "zig" / "p").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    run_root = tmp_path / "run"
    run_root.mkdir()

    env = _isolated_child_env(run_root, {"PATH": "/bin"})

    assert env["ZIG_GLOBAL_CACHE_DIR"] == str(run_root / "zig-cache")
    assert "LIBGHOSTTY_VT_ZIG_SYSTEM_DIR" not in env


def test_isolated_child_env_leaves_zig_cache_vars_unset_without_machine_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    run_root = tmp_path / "run"
    run_root.mkdir()

    env = _isolated_child_env(run_root, {"PATH": "/bin"})

    assert "ZIG_GLOBAL_CACHE_DIR" not in env
    assert "LIBGHOSTTY_VT_ZIG_SYSTEM_DIR" not in env
    assert not (run_root / "zig-cache").exists()


def test_vendored_libsystem_override_creates_tmpdir_under_run_tmpdir(tmp_path: Path) -> None:
    script = LIBSYSTEM_OVERRIDE.read_text(encoding="utf-8")
    match = re.search(r'^tmp="\$\(mktemp -d ([^)]+)\)"$', script, re.MULTILINE)
    assert match is not None, "libsystem_override.sh must pass mktemp -d an explicit template"
    line = f'tmp="$(mktemp -d {match.group(1)})"'
    completed = subprocess.run(
        ["sh", "-c", f'{line}; printf %s "$tmp"'],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": str(tmp_path)},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    created = Path(completed.stdout.strip())
    assert created.is_dir()
    assert created.is_relative_to(tmp_path)
    shutil.rmtree(created)
