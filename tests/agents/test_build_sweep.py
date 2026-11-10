"""The build sweep removes only stale build folders that no registered checkout owns."""

import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.build_root import configure_build_root
from gobby.agents.build_sweep import (
    SweepAction,
    SweepFinding,
    legacy_empty_cache_target,
    sweep_build_root,
)
from gobby.agents.cargo_target import checkout_component

pytestmark = pytest.mark.unit

NOW = 1_800_000_000.0
STALE = NOW - 3 * 24 * 3600
RECENT = NOW - 60


@pytest.fixture
def gobby_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "gobby-home"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    yield home
    configure_build_root(None)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "build"
    path.mkdir()
    return path


def _folder(path: Path, *, mtime: float) -> Path:
    """Create a Cargo-shaped build folder whose every node carries ``mtime``."""
    artifact = path / "debug" / "deps" / "libgobby.rlib"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"x" * 512)
    for node in [path, *path.rglob("*")]:
        os.utime(node, (mtime, mtime), follow_symlinks=False)
    return path


def _checkout(parent: Path, name: str) -> Path:
    path = parent / name
    path.mkdir(parents=True)
    return path


def _action_for(findings: list[SweepFinding], path: Path) -> SweepAction:
    matches = [finding.action for finding in findings if finding.path == path]
    assert len(matches) == 1, f"expected one finding for {path}, got {matches}"
    return matches[0]


@pytest.fixture
def trees(root: Path, tmp_path: Path) -> dict[str, Path]:
    """Two projects' live checkouts, a stale and a recent orphan, in every trust domain."""
    checkouts = tmp_path / "checkouts"
    live = _checkout(checkouts, "live-task")
    other = _checkout(tmp_path / "other", "project-b")
    stale = _checkout(checkouts, "gone-task")
    recent = _checkout(checkouts, "fresh-task")
    v2 = root / "cargo-target-v2"
    sandbox = root / "sandbox" / "cargo-target"
    live_component = checkout_component(live)
    stale_component = checkout_component(stale)
    return {
        "live": live,
        "other": other,
        "live_operator": _folder(v2 / "proj-a" / live_component, mtime=STALE),
        "live_agent": _folder(v2 / "proj-a" / f"{live_component}-agent", mtime=STALE),
        "live_sandbox": _folder(sandbox / live_component, mtime=STALE),
        "other_project": _folder(v2 / "proj-b" / checkout_component(other), mtime=STALE),
        "stale_operator": _folder(v2 / "proj-a" / stale_component, mtime=STALE),
        "stale_agent": _folder(v2 / "proj-a" / f"{stale_component}-agent", mtime=STALE),
        "stale_sandbox": _folder(sandbox / stale_component, mtime=STALE),
        "recent": _folder(v2 / "proj-a" / checkout_component(recent), mtime=RECENT),
    }


def test_dry_run_reports_stale_folders_and_removes_nothing(
    root: Path, trees: dict[str, Path]
) -> None:
    findings = sweep_build_root(root, [trees["live"], trees["other"]], apply=False, now=NOW)

    assert _action_for(findings, trees["stale_operator"]) is SweepAction.WOULD_REMOVE
    assert _action_for(findings, trees["stale_agent"]) is SweepAction.WOULD_REMOVE
    assert _action_for(findings, trees["stale_sandbox"]) is SweepAction.WOULD_REMOVE
    for name in ("stale_operator", "stale_agent", "stale_sandbox"):
        assert trees[name].is_dir(), name


def test_apply_removes_only_stale_unowned_folders(root: Path, trees: dict[str, Path]) -> None:
    findings = sweep_build_root(root, [trees["live"], trees["other"]], apply=True, now=NOW)

    assert _action_for(findings, trees["stale_operator"]) is SweepAction.REMOVED
    assert _action_for(findings, trees["stale_agent"]) is SweepAction.REMOVED
    assert _action_for(findings, trees["stale_sandbox"]) is SweepAction.REMOVED
    assert not trees["stale_operator"].exists()
    assert not trees["stale_agent"].exists()
    assert not trees["stale_sandbox"].exists()


def test_live_checkouts_keep_their_folders_across_projects(
    root: Path, trees: dict[str, Path]
) -> None:
    findings = sweep_build_root(root, [trees["live"], trees["other"]], apply=True, now=NOW)

    assert _action_for(findings, trees["live_operator"]) is SweepAction.KEEP_LIVE
    assert _action_for(findings, trees["live_agent"]) is SweepAction.KEEP_LIVE
    assert _action_for(findings, trees["live_sandbox"]) is SweepAction.KEEP_LIVE
    assert _action_for(findings, trees["other_project"]) is SweepAction.KEEP_LIVE
    for name in ("live_operator", "live_agent", "live_sandbox", "other_project"):
        assert trees[name].is_dir(), name


def test_recently_touched_orphans_are_kept(root: Path, trees: dict[str, Path]) -> None:
    findings = sweep_build_root(root, [trees["live"], trees["other"]], apply=True, now=NOW)

    assert _action_for(findings, trees["recent"]) is SweepAction.KEEP_RECENT
    assert trees["recent"].is_dir()


def test_symlinked_entries_are_refused_and_never_followed(
    root: Path, trees: dict[str, Path], tmp_path: Path
) -> None:
    outside = _folder(tmp_path / "outside" / "precious", mtime=STALE)
    link = root / "cargo-target-v2" / "proj-a" / "linked-elsewhere"
    link.symlink_to(outside, target_is_directory=True)

    findings = sweep_build_root(root, [trees["live"], trees["other"]], apply=True, now=NOW)

    assert _action_for(findings, link) is SweepAction.REFUSED
    assert link.is_symlink()
    assert (outside / "debug" / "deps" / "libgobby.rlib").is_file()


def test_missing_root_has_no_findings(tmp_path: Path) -> None:
    assert sweep_build_root(tmp_path / "absent", [], apply=True, now=NOW) == []


def test_empty_legacy_cache_target_is_reported_and_never_removed(gobby_home: Path) -> None:
    legacy = gobby_home / "cache" / "cargo-target"
    legacy.mkdir(parents=True)

    finding = legacy_empty_cache_target()

    assert finding is not None
    assert finding.path == legacy
    assert finding.action is SweepAction.LEGACY_EMPTY
    assert legacy.is_dir()


def test_non_empty_legacy_cache_target_is_not_reported(gobby_home: Path) -> None:
    legacy = gobby_home / "cache" / "cargo-target"
    _folder(legacy / "leftover", mtime=STALE)

    assert legacy_empty_cache_target() is None


def _executable(path: Path, *, mtime: float, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF" + b"x" * 60)
    os.chmod(path, mode)
    os.utime(path, (mtime, mtime))
    return path


def _test_unit(profile: Path, package: str, meta: str) -> None:
    """Record a Cargo test unit: its fingerprint names a test target, as Cargo writes it."""
    unit = profile / ".fingerprint" / f"{package}-{meta}"
    unit.mkdir(parents=True, exist_ok=True)
    (unit / "test-gobby_core").write_text("", encoding="utf-8")


def test_under_budget_a_live_tree_keeps_every_test_executable(
    root: Path, trees: dict[str, Path]
) -> None:
    profile = trees["live_operator"] / "debug"
    _test_unit(profile, "gobby-core", "0123456789abcdef")
    _test_unit(profile, "gobby-core", "fedcba9876543210")
    older = _executable(profile / "deps" / "gobby_core-0123456789abcdef", mtime=STALE)
    newer = _executable(profile / "deps" / "gobby_core-fedcba9876543210", mtime=STALE + 3600)

    findings = sweep_build_root(root, [trees["live"], trees["other"]], apply=True, now=NOW)

    assert older.is_file()
    assert newer.is_file()
    assert not [finding for finding in findings if finding.path in (older, newer)]


def test_over_budget_the_oldest_test_executable_goes_first(
    root: Path, trees: dict[str, Path]
) -> None:
    profile = trees["live_operator"] / "debug"
    _test_unit(profile, "gobby-core", "0123456789abcdef")
    _test_unit(profile, "gobby-core", "fedcba9876543210")
    older = _executable(profile / "deps" / "gobby_core-0123456789abcdef", mtime=STALE)
    newer = _executable(profile / "deps" / "gobby_core-fedcba9876543210", mtime=STALE + 3600)

    findings = sweep_build_root(
        root, [trees["live"], trees["other"]], apply=True, now=NOW, executable_budget_bytes=64
    )

    assert _action_for(findings, older) is SweepAction.REMOVED
    assert not older.exists()
    assert newer.is_file()


def test_a_variant_built_within_the_quiet_window_survives_an_over_budget_trim(
    root: Path, trees: dict[str, Path]
) -> None:
    profile = trees["live_operator"] / "debug"
    _test_unit(profile, "gobby-core", "0123456789abcdef")
    _test_unit(profile, "gobby-core", "fedcba9876543210")
    stale_variant = _executable(profile / "deps" / "gobby_core-0123456789abcdef", mtime=STALE)
    live_variant = _executable(profile / "deps" / "gobby_core-fedcba9876543210", mtime=RECENT)

    findings = sweep_build_root(
        root, [trees["live"], trees["other"]], apply=True, now=NOW, executable_budget_bytes=0
    )

    assert _action_for(findings, stale_variant) is SweepAction.REMOVED
    assert _action_for(findings, live_variant) is SweepAction.KEEP_RECENT
    assert live_variant.is_file()


def test_bin_executables_are_never_trimmed(root: Path, trees: dict[str, Path]) -> None:
    profile = trees["live_operator"] / "debug"
    bin_unit = profile / ".fingerprint" / "gobby-core-0123456789abcdef"
    bin_unit.mkdir(parents=True)
    (bin_unit / "bin-gobby_core").write_text("", encoding="utf-8")
    binary = _executable(profile / "deps" / "gobby_core-0123456789abcdef", mtime=STALE)

    findings = sweep_build_root(
        root, [trees["live"], trees["other"]], apply=True, now=NOW, executable_budget_bytes=0
    )

    assert binary.is_file()
    assert not [finding for finding in findings if finding.path == binary]


def test_dry_run_reports_the_trim_without_removing(root: Path, trees: dict[str, Path]) -> None:
    profile = trees["live_operator"] / "debug"
    _test_unit(profile, "gobby-core", "0123456789abcdef")
    _test_unit(profile, "gobby-core", "fedcba9876543210")
    older = _executable(profile / "deps" / "gobby_core-0123456789abcdef", mtime=STALE)
    _executable(profile / "deps" / "gobby_core-fedcba9876543210", mtime=STALE + 3600)

    findings = sweep_build_root(
        root, [trees["live"], trees["other"]], apply=False, now=NOW, executable_budget_bytes=64
    )

    assert _action_for(findings, older) is SweepAction.WOULD_REMOVE
    assert older.is_file()


def test_prune_ignores_non_executables_and_unhashed_names(
    root: Path, trees: dict[str, Path]
) -> None:
    profile = trees["live_operator"] / "debug"
    _test_unit(profile, "gobby-core", "0123456789abcdef")
    _test_unit(profile, "gobby-core", "fedcba9876543210")
    deps = profile / "deps"
    data = _executable(deps / "gobby_core-0123456789abcdef", mtime=STALE, mode=0o644)
    unhashed = _executable(deps / "gobby_core", mtime=STALE)
    trimmed = _executable(deps / "gobby_core-fedcba9876543210", mtime=STALE + 3600)

    findings = sweep_build_root(
        root, [trees["live"], trees["other"]], apply=True, now=NOW, executable_budget_bytes=0
    )

    assert _action_for(findings, trimmed) is SweepAction.REMOVED
    assert data.is_file()
    assert unhashed.is_file()
    assert not [finding for finding in findings if finding.path in (data, unhashed)]


@pytest.mark.parametrize("profile", ["debug", "release"])
def test_a_profile_dir_linked_outside_the_root_is_never_trimmed(
    root: Path, trees: dict[str, Path], tmp_path: Path, profile: str
) -> None:
    outside = tmp_path / "outside" / profile
    _test_unit(outside, "gobby-core", "0123456789abcdef")
    victim = _executable(outside / "deps" / "gobby_core-0123456789abcdef", mtime=STALE)
    tree = root / "cargo-target-v2" / "proj-c" / f"{checkout_component(trees['live'])}-agent"
    tree.mkdir(parents=True)
    link = tree / profile
    link.symlink_to(outside, target_is_directory=True)

    findings = sweep_build_root(
        root, [trees["live"], trees["other"]], apply=True, now=NOW, executable_budget_bytes=0
    )

    assert victim.is_file()
    assert link.is_symlink()
    assert _action_for(findings, link / "deps") is SweepAction.REFUSED
    assert not [finding for finding in findings if finding.path == victim]


def test_a_sandbox_root_linked_outside_the_root_is_never_swept(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-sandbox"
    stale = _folder(outside / "cargo-target" / "gone-task", mtime=STALE)
    (root / "sandbox").symlink_to(outside, target_is_directory=True)

    sweep_build_root(root, [], apply=True, now=NOW)

    assert stale.is_dir()


def test_a_symlinked_build_root_is_refused_whole(tmp_path: Path) -> None:
    real = tmp_path / "real-build"
    stale = _folder(real / "cargo-target-v2" / "proj-a" / "gone-task", mtime=STALE)
    link = tmp_path / "linked-build"
    link.symlink_to(real, target_is_directory=True)

    findings = sweep_build_root(link, [], apply=True, now=NOW)

    assert stale.is_dir()
    assert _action_for(findings, link) is SweepAction.REFUSED
    assert len(findings) == 1


def test_deps_swapped_for_a_link_after_the_scan_never_redirects_the_trim(
    root: Path, trees: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sandboxed build owns its tree, so it can swap deps for a link between scan and unlink."""
    name = "gobby_core-0123456789abcdef"
    outside = tmp_path / "outside" / "debug" / "deps"
    victim = _executable(outside / name, mtime=STALE)
    profile = trees["live_agent"] / "debug"
    _test_unit(profile, "gobby-core", "0123456789abcdef")
    deps = profile / "deps"
    _executable(deps / name, mtime=STALE)
    moved = profile / "deps-moved"
    real_unlink = os.unlink

    def swap_then_unlink(path: str | Path, *args: Any, **kwargs: Any) -> None:
        # Swap only before the trim's own unlink; rmtree of stale folders also unlinks.
        if os.path.basename(path) == name and not deps.is_symlink():
            deps.rename(moved)
            deps.symlink_to(outside, target_is_directory=True)
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("gobby.agents.build_sweep.os.unlink", swap_then_unlink)

    findings = sweep_build_root(
        root, [trees["live"], trees["other"]], apply=True, now=NOW, executable_budget_bytes=0
    )

    assert victim.is_file()
    assert not (moved / name).exists()
    assert _action_for(findings, deps / name) is SweepAction.REMOVED


def test_an_unowned_folder_swapped_for_a_link_is_never_removed_through_it(
    root: Path, trees: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = _folder(tmp_path / "outside-tree", mtime=STALE)
    stale = trees["stale_agent"]
    real_rmtree = shutil.rmtree

    def swap_then_rmtree(path: str | Path, *args: Any, **kwargs: Any) -> None:
        # Swap only before the sweep's own removal of the stale folder.
        if os.path.basename(path) == stale.name and not stale.is_symlink():
            real_rmtree(stale)
            stale.symlink_to(outside, target_is_directory=True)
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("gobby.agents.build_sweep.shutil.rmtree", swap_then_rmtree)

    findings = sweep_build_root(root, [trees["live"], trees["other"]], apply=True, now=NOW)

    assert (outside / "debug" / "deps" / "libgobby.rlib").is_file()
    assert _action_for(findings, stale) is SweepAction.REFUSED
