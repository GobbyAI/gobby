from __future__ import annotations

from pathlib import Path

import pytest

from gobby.build.input_resolution import plan_file_base_dir, resolve_plan_file_path
from gobby.build.options import BuildOptions
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import CheckoutNotFoundError
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    insert_overlay,
    install_isolated_checkout_project,
    patch_local_machine_id,
)

pytestmark = pytest.mark.unit


def _checkout_project(
    temp_db: HubDatabase,
    repo_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str = "build-project",
) -> str:
    isolated = install_isolated_checkout_project(
        temp_db, repo_path, name=name, monkeypatch=monkeypatch
    )
    return isolated.project.id


def test_resolve_plan_file_path_uses_machine_checkout(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo"
    plans_dir = repo_path / ".gobby" / "plans"
    plans_dir.mkdir(parents=True)
    plan_file = plans_dir / "plan.md"
    plan_file.write_text("# Plan\n", encoding="utf-8")
    project_id = _checkout_project(temp_db, repo_path, monkeypatch)

    resolved = resolve_plan_file_path(
        "plan.md",
        LocalTaskManager(temp_db),
        project_id,
        BuildOptions(),
    )

    assert resolved == plan_file.resolve()


def test_resolve_plan_file_path_accepts_project_relative_plan(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo"
    plans_dir = repo_path / ".gobby" / "plans"
    plans_dir.mkdir(parents=True)
    plan_file = plans_dir / "plan.md"
    plan_file.write_text("# Plan\n", encoding="utf-8")
    project_id = _checkout_project(temp_db, repo_path, monkeypatch)

    resolved = resolve_plan_file_path(
        "plan.md",
        LocalTaskManager(temp_db),
        project_id,
        BuildOptions(),
    )

    assert resolved == plan_file.resolve()


def test_resolve_plan_file_path_prefers_registered_overlay(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "primary"
    isolated = install_isolated_checkout_project(
        temp_db, primary, name="overlay-build", monkeypatch=monkeypatch
    )
    overlay = tmp_path / "worktree"
    plans_dir = overlay / ".gobby" / "plans"
    plans_dir.mkdir(parents=True)
    plan_file = plans_dir / "plan.md"
    plan_file.write_text("# Overlay plan\n", encoding="utf-8")
    insert_overlay(
        temp_db,
        project_id=isolated.project.id,
        machine_id=isolated.machine_id,
        path=str(overlay),
        kind="worktree",
    )

    resolved = resolve_plan_file_path(
        "plan.md",
        LocalTaskManager(temp_db),
        isolated.project.id,
        BuildOptions(cwd=overlay),
    )

    assert resolved == plan_file.resolve()


def test_plan_file_base_dir_fails_closed_without_checkout(  # tdd-red window
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="missing-checkout-build")

    with pytest.raises(CheckoutNotFoundError):
        plan_file_base_dir(
            LocalTaskManager(temp_db),
            project.id,
            BuildOptions(),
        )


def test_resolve_plan_file_path_rejects_escaping_path(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    project_id = _checkout_project(temp_db, repo_path, monkeypatch, name="escape-build")

    with pytest.raises(ValueError, match="plan file must stay inside"):
        resolve_plan_file_path(
            "../outside.md",
            LocalTaskManager(temp_db),
            project_id,
            BuildOptions(),
        )
