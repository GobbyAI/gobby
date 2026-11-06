"""The build sweep removes only stale build folders that no registered checkout owns."""

import os
from collections.abc import Iterator
from pathlib import Path

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


def test_live_tree_prunes_superseded_executables_and_keeps_the_newest(
    root: Path, trees: dict[str, Path]
) -> None:
    deps = trees["live_operator"] / "debug" / "deps"
    superseded = _executable(deps / "gobby_core-0123456789abcdef", mtime=STALE)
    newest = _executable(deps / "gobby_core-fedcba9876543210", mtime=STALE + 3600)

    findings = sweep_build_root(root, [trees["live"], trees["other"]], apply=True, now=NOW)

    assert _action_for(findings, superseded) is SweepAction.REMOVED
    assert not superseded.exists()
    assert newest.is_file()


def test_recent_superseded_executables_stay_in_a_live_tree(
    root: Path, trees: dict[str, Path]
) -> None:
    deps = trees["live_operator"] / "debug" / "deps"
    older = _executable(deps / "gobby_core-0123456789abcdef", mtime=RECENT - 120)
    newest = _executable(deps / "gobby_core-fedcba9876543210", mtime=RECENT)

    findings = sweep_build_root(root, [trees["live"], trees["other"]], apply=True, now=NOW)

    assert _action_for(findings, older) is SweepAction.KEEP_RECENT
    assert older.is_file()
    assert newest.is_file()


def test_dry_run_reports_superseded_executables_without_removing(
    root: Path, trees: dict[str, Path]
) -> None:
    deps = trees["live_operator"] / "debug" / "deps"
    superseded = _executable(deps / "gobby_core-0123456789abcdef", mtime=STALE)
    _executable(deps / "gobby_core-fedcba9876543210", mtime=STALE + 3600)

    findings = sweep_build_root(root, [trees["live"], trees["other"]], apply=False, now=NOW)

    assert _action_for(findings, superseded) is SweepAction.WOULD_REMOVE
    assert superseded.is_file()


def test_prune_ignores_non_executables_and_unhashed_names(
    root: Path, trees: dict[str, Path]
) -> None:
    deps = trees["live_operator"] / "debug" / "deps"
    data = _executable(deps / "gobby_core-0123456789abcdef", mtime=STALE, mode=0o644)
    unhashed = _executable(deps / "gobby_core", mtime=STALE)
    _executable(deps / "gobby_core-fedcba9876543210", mtime=STALE + 3600)

    findings = sweep_build_root(root, [trees["live"], trees["other"]], apply=True, now=NOW)

    assert data.is_file()
    assert unhashed.is_file()
    assert not [finding for finding in findings if finding.path in (data, unhashed)]
