"""Tests for provider-aware plan-mode scratch paths."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gobby.providers.path_policy import is_plan_scratch_path, is_project_plan_artifact_path

pytestmark = pytest.mark.unit

PROVIDER_DIRECTORIES = {
    "claude": ".claude",
    "codex": ".codex",
    "droid": ".factory",
    "grok": ".grok",
    "qwen": ".qwen",
    "agy": ".gemini",
}


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    temp = tmp_path / "temp"
    home.mkdir()
    temp.mkdir()
    return home, temp


def test_project_plan_artifact_requires_gobby_plans_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    assert is_project_plan_artifact_path(".gobby/plans/design.md", repo)
    assert not is_project_plan_artifact_path(".claude/plans/design.md", repo)
    assert not is_project_plan_artifact_path(".codex/plans/design.md", repo)
    assert not is_project_plan_artifact_path(".gobby/notes/design.md", repo)
    assert not is_project_plan_artifact_path("../outside/.gobby/plans/design.md", repo)


@pytest.mark.parametrize(("provider", "directory"), PROVIDER_DIRECTORIES.items())
def test_accepts_active_provider_directory(
    tmp_path: Path,
    provider: str,
    directory: str,
) -> None:
    home, temp = _roots(tmp_path)
    absolute = home / directory / "scratch" / "state.json"

    assert is_plan_scratch_path(
        str(absolute),
        provider,
        home_root=home,
        temp_root=temp,
        posix_tmp_root=None,
    )
    assert is_plan_scratch_path(
        f"~/{directory}/scratch/state.json",
        provider,
        home_root=home,
        temp_root=temp,
        posix_tmp_root=None,
    )


@pytest.mark.parametrize("provider", PROVIDER_DIRECTORIES)
def test_accepts_injected_os_temp_for_every_provider(tmp_path: Path, provider: str) -> None:
    home, temp = _roots(tmp_path)

    assert is_plan_scratch_path(
        str(temp / "gobby" / "scratch.bin"),
        provider,
        home_root=home,
        temp_root=temp,
        posix_tmp_root=None,
    )


def test_rejects_inactive_provider_directory_when_home_is_under_temp(tmp_path: Path) -> None:
    temp = tmp_path / "system-temp"
    home = temp / "home"
    home.mkdir(parents=True)

    assert is_plan_scratch_path(
        str(home / ".gemini" / "scratch" / "state.json"),
        "agy",
        home_root=home,
        temp_root=temp,
        posix_tmp_root=None,
    )
    assert not is_plan_scratch_path(
        str(home / ".claude" / "scratch" / "state.json"),
        "agy",
        home_root=home,
        temp_root=temp,
        posix_tmp_root=None,
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX /tmp policy")
def test_accepts_resolved_posix_tmp(tmp_path: Path) -> None:
    home, temp = _roots(tmp_path)

    assert is_plan_scratch_path(
        "/tmp/gobby-plan-scratch/test.json",
        "codex",
        home_root=home,
        temp_root=temp,
        posix_tmp_root="/tmp",
    )


@pytest.mark.parametrize(
    "candidate",
    [
        ".codex/scratch.json",
        "project/.codex/scratch.json",
        "../.codex/scratch.json",
    ],
)
def test_rejects_project_relative_paths(tmp_path: Path, candidate: str) -> None:
    home, temp = _roots(tmp_path)

    assert not is_plan_scratch_path(
        candidate,
        "codex",
        home_root=home,
        temp_root=temp,
        posix_tmp_root=None,
    )


def test_rejects_cross_provider_prefix_collision_traversal_and_unknown_provider(
    tmp_path: Path,
) -> None:
    home, temp = _roots(tmp_path)
    candidates = [
        (home / ".claude" / "scratch.json", "codex"),
        (home / ".codex-other" / "scratch.json", "codex"),
        (home / ".codex" / ".." / "notes.json", "codex"),
        (home / ".codex" / "scratch.json", "unsupported"),
        (tmp_path / "repo" / "notes.json", "codex"),
    ]

    for candidate, provider in candidates:
        assert not is_plan_scratch_path(
            str(candidate),
            provider,
            home_root=home,
            temp_root=temp,
            posix_tmp_root=None,
        )


@pytest.mark.skipif(os.name == "nt", reason="symlink creation can require elevation on Windows")
def test_rejects_symlink_escape_from_provider_directory(tmp_path: Path) -> None:
    home, temp = _roots(tmp_path)
    provider_root = home / ".codex"
    outside = tmp_path / "outside"
    provider_root.mkdir()
    outside.mkdir()
    (provider_root / "escape").symlink_to(outside, target_is_directory=True)

    assert not is_plan_scratch_path(
        str(provider_root / "escape" / "state.json"),
        "codex",
        home_root=home,
        temp_root=temp,
        posix_tmp_root=None,
    )


@pytest.mark.skipif(os.name == "nt", reason="symlink creation can require elevation on Windows")
def test_rejects_symlinked_provider_directory(tmp_path: Path) -> None:
    home, temp = _roots(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".codex").symlink_to(outside, target_is_directory=True)

    assert not is_plan_scratch_path(
        str(home / ".codex" / "state.json"),
        "codex",
        home_root=home,
        temp_root=temp,
        posix_tmp_root=None,
    )
