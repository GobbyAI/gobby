"""Checkout-specific Cargo target directories and links."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from gobby.agents.cargo_target import (
    checkout_cargo_target_dir,
    cleanup_checkout_cargo_target_dir,
    ensure_checkout_cargo_target_dir,
    exclude_checkout_target,
    link_checkout_cargo_target,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def gobby_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    return home


@pytest.fixture
def cargo_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    return checkout


def test_checkout_dir_is_deterministic_under_project_cache(
    gobby_home: Path, cargo_checkout: Path
) -> None:
    canonical = cargo_checkout.resolve()
    suffix = hashlib.sha256(os.fsencode(canonical)).hexdigest()[:16]

    first = checkout_cargo_target_dir(cargo_checkout, "proj-1")
    second = checkout_cargo_target_dir(cargo_checkout, "proj-1")

    assert first == second
    assert first == gobby_home / "cache" / "cargo-target-v2" / "proj-1" / (f"checkout-{suffix}")


def test_checkout_dirs_are_distinct(gobby_home: Path, tmp_path: Path) -> None:
    first = tmp_path / "main"
    second = tmp_path / "worktree"
    first.mkdir()
    second.mkdir()

    assert checkout_cargo_target_dir(first, "proj-1") != checkout_cargo_target_dir(second, "proj-1")


@pytest.mark.parametrize(
    ("project_id", "leaf"),
    [
        ("a/b:c d", "a-b-c-d"),
        ("///", "unknown-project"),
        ("", "unknown-project"),
        ("x" * 100, "x" * 80),
    ],
)
def test_checkout_dir_sanitizes_project_id(
    gobby_home: Path, cargo_checkout: Path, project_id: str, leaf: str
) -> None:
    assert checkout_cargo_target_dir(cargo_checkout, project_id).parent.name == leaf


def test_checkout_dir_sanitizes_checkout_name(gobby_home: Path, tmp_path: Path) -> None:
    checkout = tmp_path / "a checkout:name"
    checkout.mkdir()

    assert checkout_cargo_target_dir(checkout, "proj-1").name.startswith("a-checkout-name-")


def test_ensure_creates_checkout_dir(gobby_home: Path, cargo_checkout: Path) -> None:
    path = ensure_checkout_cargo_target_dir(cargo_checkout, "proj-1")

    assert path == str(checkout_cargo_target_dir(cargo_checkout, "proj-1"))
    assert Path(path).is_dir()


def test_link_created_for_cargo_checkout(gobby_home: Path, cargo_checkout: Path) -> None:
    assert link_checkout_cargo_target(cargo_checkout, "proj-1") is True

    link = cargo_checkout / "target"
    checkout_target = checkout_cargo_target_dir(cargo_checkout, "proj-1")
    assert link.is_symlink()
    assert os.readlink(link) == str(checkout_target)
    assert checkout_target.is_dir()


def test_link_is_idempotent(gobby_home: Path, cargo_checkout: Path) -> None:
    assert link_checkout_cargo_target(cargo_checkout, "proj-1") is True
    first_stat = os.lstat(cargo_checkout / "target")

    assert link_checkout_cargo_target(cargo_checkout, "proj-1") is True

    assert os.lstat(cargo_checkout / "target") == first_stat


def test_legacy_link_is_atomically_migrated_without_artifacts(
    gobby_home: Path,
    cargo_checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = gobby_home / "cache" / "cargo-target" / "proj-1"
    legacy.mkdir(parents=True)
    (legacy / "contaminated").write_text("old", encoding="utf-8")
    target = cargo_checkout / "target"
    os.symlink(legacy, target, target_is_directory=True)
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def record_replace(source: Path, destination: Path) -> None:
        replacements.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr("gobby.agents.cargo_target.os.replace", record_replace)

    assert link_checkout_cargo_target(cargo_checkout, "proj-1") is True

    desired = checkout_cargo_target_dir(cargo_checkout, "proj-1")
    assert os.readlink(target) == str(desired)
    assert replacements and replacements[0][1] == target
    assert not (desired / "contaminated").exists()
    assert (legacy / "contaminated").read_text(encoding="utf-8") == "old"


def test_non_cargo_checkout_untouched(gobby_home: Path, tmp_path: Path) -> None:
    checkout = tmp_path / "plain"
    checkout.mkdir()

    assert link_checkout_cargo_target(checkout, "proj-1") is False

    assert not (checkout / "target").is_symlink()
    assert not (checkout / "target").exists()
    assert not (gobby_home / "cache").exists()


def test_existing_real_target_dir_left_alone(
    gobby_home: Path, cargo_checkout: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = cargo_checkout / "target"
    (target / "debug").mkdir(parents=True)
    (target / "debug" / "artifact").write_text("built", encoding="utf-8")

    with caplog.at_level("DEBUG", logger="gobby.agents.cargo_target"):
        assert link_checkout_cargo_target(cargo_checkout, "proj-1") is False

    assert not target.is_symlink()
    assert (target / "debug" / "artifact").read_text(encoding="utf-8") == "built"
    assert any("existing target directory" in record.getMessage() for record in caplog.records)


def test_foreign_symlink_left_alone(gobby_home: Path, cargo_checkout: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = cargo_checkout / "target"
    os.symlink(elsewhere, target, target_is_directory=True)

    assert link_checkout_cargo_target(cargo_checkout, "proj-1") is False

    assert os.readlink(target) == str(elsewhere)


def test_cleanup_removes_only_derived_checkout_target(
    gobby_home: Path, cargo_checkout: Path
) -> None:
    target = checkout_cargo_target_dir(cargo_checkout, "proj-1")
    (target / "debug").mkdir(parents=True)

    assert cleanup_checkout_cargo_target_dir(cargo_checkout, "proj-1") is None
    assert not target.exists()
    assert cleanup_checkout_cargo_target_dir(cargo_checkout, "proj-1") is None


def test_cleanup_refuses_symlinked_cache_target(
    gobby_home: Path, cargo_checkout: Path, tmp_path: Path
) -> None:
    target = checkout_cargo_target_dir(cargo_checkout, "proj-1")
    target.parent.mkdir(parents=True)
    foreign = tmp_path / "foreign-cache"
    foreign.mkdir()
    os.symlink(foreign, target, target_is_directory=True)

    error = cleanup_checkout_cargo_target_dir(cargo_checkout, "proj-1")

    assert error == f"Refusing to remove symlinked Cargo target path: {target}"
    assert target.is_symlink()
    assert foreign.is_dir()


def test_cleanup_refuses_symlinked_project_cache_root(
    gobby_home: Path, cargo_checkout: Path, tmp_path: Path
) -> None:
    project_root = gobby_home / "cache" / "cargo-target-v2" / "proj-1"
    project_root.parent.mkdir(parents=True)
    foreign = tmp_path / "foreign-project-cache"
    target = checkout_cargo_target_dir(cargo_checkout, "proj-1")
    (foreign / target.name).mkdir(parents=True)
    sentinel = foreign / target.name / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    os.symlink(foreign, project_root, target_is_directory=True)

    error = cleanup_checkout_cargo_target_dir(cargo_checkout, "proj-1")

    assert error == f"Refusing to remove Cargo target below symlinked project cache: {project_root}"
    assert project_root.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_symlink_failure_is_logged_not_raised(
    gobby_home: Path,
    cargo_checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only checkout")

    monkeypatch.setattr("gobby.agents.cargo_target.os.symlink", _refuse)

    with caplog.at_level("WARNING", logger="gobby.agents.cargo_target"):
        assert link_checkout_cargo_target(cargo_checkout, "proj-1") is False

    assert not (cargo_checkout / "target").is_symlink()
    assert any("Failed to link" in record.getMessage() for record in caplog.records)


def _git_dir_checkout(cargo_checkout: Path) -> Path:
    (cargo_checkout / ".git" / "info").mkdir(parents=True)
    return cargo_checkout


def test_link_excludes_target_in_a_primary_checkout(gobby_home: Path, cargo_checkout: Path) -> None:
    _git_dir_checkout(cargo_checkout)

    assert link_checkout_cargo_target(cargo_checkout, "proj-1") is True

    exclude = cargo_checkout / ".git" / "info" / "exclude"
    assert exclude.read_text(encoding="utf-8").splitlines() == ["/target"]


def test_exclude_appends_after_existing_patterns(gobby_home: Path, cargo_checkout: Path) -> None:
    _git_dir_checkout(cargo_checkout)
    exclude = cargo_checkout / ".git" / "info" / "exclude"
    exclude.write_text("# local\n.env\n", encoding="utf-8")

    assert exclude_checkout_target(cargo_checkout) is True

    assert exclude.read_text(encoding="utf-8").splitlines() == ["# local", ".env", "/target"]


def test_exclude_terminates_an_unterminated_final_line(
    gobby_home: Path, cargo_checkout: Path
) -> None:
    _git_dir_checkout(cargo_checkout)
    exclude = cargo_checkout / ".git" / "info" / "exclude"
    exclude.write_text(".env", encoding="utf-8")

    assert exclude_checkout_target(cargo_checkout) is True

    assert exclude.read_text(encoding="utf-8") == ".env\n/target\n"


def test_exclude_is_idempotent(gobby_home: Path, cargo_checkout: Path) -> None:
    _git_dir_checkout(cargo_checkout)
    exclude = cargo_checkout / ".git" / "info" / "exclude"

    assert exclude_checkout_target(cargo_checkout) is True
    assert exclude_checkout_target(cargo_checkout) is True

    assert exclude.read_text(encoding="utf-8") == "/target\n"


def test_linked_worktree_excludes_through_the_shared_repository(
    gobby_home: Path, tmp_path: Path
) -> None:
    """A worktree's `.git` file points at a per-worktree dir sharing one exclude file."""
    repo_git = tmp_path / "repo" / ".git"
    (repo_git / "info").mkdir(parents=True)
    worktree_git = repo_git / "worktrees" / "feature"
    worktree_git.mkdir(parents=True)
    (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = tmp_path / "feature"
    worktree.mkdir()
    (worktree / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {worktree_git}\n", encoding="utf-8")

    assert link_checkout_cargo_target(worktree, "proj-1") is True

    assert (repo_git / "info" / "exclude").read_text(encoding="utf-8") == "/target\n"
    assert not (worktree_git / "info" / "exclude").exists()


def test_checkout_outside_a_work_tree_is_left_alone(gobby_home: Path, cargo_checkout: Path) -> None:
    assert exclude_checkout_target(cargo_checkout) is False
    assert link_checkout_cargo_target(cargo_checkout, "proj-1") is True
    assert (cargo_checkout / "target").is_symlink()


def test_exclude_failure_is_logged_not_raised(
    gobby_home: Path,
    cargo_checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _git_dir_checkout(cargo_checkout)

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only repository")

    monkeypatch.setattr(Path, "write_text", _refuse)

    with caplog.at_level("WARNING", logger="gobby.agents.cargo_target"):
        assert exclude_checkout_target(cargo_checkout) is False

    assert any("Failed to exclude" in record.getMessage() for record in caplog.records)


def test_dangling_worktree_pointer_creates_nothing(gobby_home: Path, cargo_checkout: Path) -> None:
    """A `.git` file naming a missing repository never has directories built under it."""
    missing = cargo_checkout.parent / "gone" / "worktrees" / "feature"
    (cargo_checkout / ".git").write_text(f"gitdir: {missing}\n", encoding="utf-8")

    assert exclude_checkout_target(cargo_checkout) is False

    assert not missing.exists()
    assert not (cargo_checkout.parent / "gone").exists()
