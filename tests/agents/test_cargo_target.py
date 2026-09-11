"""Shared per-project cargo target directory and checkout links."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gobby.agents.cargo_target import (
    ensure_shared_cargo_target_dir,
    link_checkout_cargo_target,
    shared_cargo_target_dir,
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


def test_shared_dir_lives_under_gobby_home_cache(gobby_home: Path) -> None:
    assert shared_cargo_target_dir("proj-1") == gobby_home / "cache" / "cargo-target" / "proj-1"


@pytest.mark.parametrize(
    ("project_id", "leaf"),
    [
        ("a/b:c d", "a-b-c-d"),
        ("///", "unknown-project"),
        ("", "unknown-project"),
        ("x" * 100, "x" * 80),
    ],
)
def test_shared_dir_sanitizes_project_id(gobby_home: Path, project_id: str, leaf: str) -> None:
    assert shared_cargo_target_dir(project_id).name == leaf


def test_ensure_creates_shared_dir(gobby_home: Path) -> None:
    path = ensure_shared_cargo_target_dir("proj-1")

    assert path == str(gobby_home / "cache" / "cargo-target" / "proj-1")
    assert Path(path).is_dir()


def test_link_created_for_cargo_checkout(gobby_home: Path, cargo_checkout: Path) -> None:
    assert link_checkout_cargo_target(cargo_checkout, "proj-1") is True

    link = cargo_checkout / "target"
    shared = gobby_home / "cache" / "cargo-target" / "proj-1"
    assert link.is_symlink()
    assert os.readlink(link) == str(shared)
    assert shared.is_dir()


def test_link_is_idempotent(gobby_home: Path, cargo_checkout: Path) -> None:
    assert link_checkout_cargo_target(cargo_checkout, "proj-1") is True
    first_stat = os.lstat(cargo_checkout / "target")

    assert link_checkout_cargo_target(cargo_checkout, "proj-1") is True

    assert os.lstat(cargo_checkout / "target") == first_stat


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
    assert any("move it aside" in record.getMessage() for record in caplog.records)


def test_foreign_symlink_left_alone(gobby_home: Path, cargo_checkout: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = cargo_checkout / "target"
    os.symlink(elsewhere, target, target_is_directory=True)

    assert link_checkout_cargo_target(cargo_checkout, "proj-1") is False

    assert os.readlink(target) == str(elsewhere)


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
