"""Tests for validate_checkout_root and checkout-writer machine gating."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import OverlayRegistrationRejectedError
from gobby.storage.projects import LocalProjectManager
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.utils.checkout_root import (
    InvalidCheckoutRootError,
    MarkerMismatchError,
    validate_checkout_root,
)
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    insert_overlay,
    install_isolated_checkout_project,
    patch_local_machine_id,
    write_project_marker,
)

pytestmark = pytest.mark.integration


def test_validate_checkout_root_rejects_relative_path(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    validate = validate_checkout_root
    with pytest.raises(InvalidCheckoutRootError):
        validate(
            temp_db,
            project_id=isolated.project.id,
            machine_id=isolated.machine_id,
            candidate_path="relative/repo",
            expected_marker_id=isolated.project.id,
        )


def test_validate_checkout_root_rejects_unexpanded_tilde(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~` is a path-shape error even when HOME would expand."""
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    validate = validate_checkout_root
    with pytest.raises(InvalidCheckoutRootError):
        validate(
            temp_db,
            project_id=isolated.project.id,
            machine_id=isolated.machine_id,
            candidate_path="~/Projects/gobby",
            expected_marker_id=isolated.project.id,
        )


def test_validate_checkout_root_does_not_expand_existing_home_path(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing HOME expansion is still rejected as a tilde-shaped path."""
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    home = tmp_path / "home"
    expanded = home / "Projects" / "gobby"
    expanded.mkdir(parents=True)
    write_project_marker(expanded, project_id=isolated.project.id, name="test-project")
    monkeypatch.setenv("HOME", str(home))
    tilde_path = "~/Projects/gobby"
    assert os.path.expanduser(tilde_path) == str(expanded)
    with pytest.raises(InvalidCheckoutRootError):
        validate_checkout_root(
            temp_db,
            project_id=isolated.project.id,
            machine_id=isolated.machine_id,
            candidate_path=tilde_path,
            expected_marker_id=isolated.project.id,
        )


def test_validate_checkout_root_rejects_nonexistent_path(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    missing = tmp_path / "missing-root"
    validate = validate_checkout_root
    with pytest.raises(InvalidCheckoutRootError):
        validate(
            temp_db,
            project_id=isolated.project.id,
            machine_id=isolated.machine_id,
            candidate_path=str(missing),
            expected_marker_id=isolated.project.id,
        )


def test_validate_checkout_root_rejects_non_normalized_absolute(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    dotted = f"{isolated.root_path}/../{Path(isolated.root_path).name}"
    assert os.path.normpath(dotted) != dotted
    validate = validate_checkout_root
    with pytest.raises(InvalidCheckoutRootError):
        validate(
            temp_db,
            project_id=isolated.project.id,
            machine_id=isolated.machine_id,
            candidate_path=dotted,
            expected_marker_id=isolated.project.id,
        )


def test_validate_checkout_root_rejects_overlay(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    overlay = tmp_path / "wt"
    overlay.mkdir()
    write_project_marker(overlay, project_id=isolated.project.id, name="test-project")
    insert_overlay(
        temp_db,
        project_id=isolated.project.id,
        machine_id=isolated.machine_id,
        path=str(overlay),
        kind="worktree",
    )
    validate = validate_checkout_root
    with pytest.raises(OverlayRegistrationRejectedError):
        validate(
            temp_db,
            project_id=isolated.project.id,
            machine_id=isolated.machine_id,
            candidate_path=str(overlay),
            expected_marker_id=isolated.project.id,
        )


def test_validate_checkout_root_rejects_marker_mismatch(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    other = tmp_path / "other"
    other.mkdir()
    write_project_marker(other, project_id=str(uuid.uuid4()), name="other")
    validate = validate_checkout_root
    with pytest.raises(MarkerMismatchError):
        validate(
            temp_db,
            project_id=isolated.project.id,
            machine_id=isolated.machine_id,
            candidate_path=str(other),
            expected_marker_id=isolated.project.id,
        )


def test_validate_checkout_root_accepts_normalized_absolute(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    validate = validate_checkout_root
    assert (
        validate(
            temp_db,
            project_id=isolated.project.id,
            machine_id=isolated.machine_id,
            candidate_path=isolated.root_path,
            expected_marker_id=isolated.project.id,
        )
        == isolated.root_path
    )


def test_validate_checkout_root_ignores_foreign_machine_overlay_at_same_path(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    foreign = insert_isolated_machine(temp_db)
    insert_overlay(
        temp_db,
        project_id=isolated.project.id,
        machine_id=foreign,
        path=isolated.root_path,
        kind="worktree",
    )
    validate = validate_checkout_root
    assert (
        validate(
            temp_db,
            project_id=isolated.project.id,
            machine_id=isolated.machine_id,
            candidate_path=isolated.root_path,
            expected_marker_id=isolated.project.id,
        )
        == isolated.root_path
    )


def test_validate_checkout_root_refuses_same_machine_overlay(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    overlay = tmp_path / "clone"
    overlay.mkdir()
    write_project_marker(overlay, project_id=isolated.project.id, name="test-project")
    insert_overlay(
        temp_db,
        project_id=isolated.project.id,
        machine_id=isolated.machine_id,
        path=str(overlay),
        kind="clone",
    )
    validate = validate_checkout_root
    with pytest.raises(OverlayRegistrationRejectedError):
        validate(
            temp_db,
            project_id=isolated.project.id,
            machine_id=isolated.machine_id,
            candidate_path=str(overlay),
            expected_marker_id=isolated.project.id,
        )


def test_create_uses_local_machine_when_provided_machine_id_is_none(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.storage.project_checkouts import LocalProjectCheckoutManager

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    root = tmp_path / "created"
    root.mkdir()
    project_id = str(uuid.uuid4())
    write_project_marker(root, project_id=project_id, name="created-none")
    monkeypatch.setattr("gobby.storage.projects.uuid.uuid4", lambda: uuid.UUID(project_id))
    validate_calls: list[str] = []
    real_validate = validate_checkout_root

    def _wrap(*args: Any, **kwargs: Any) -> str:
        validate_calls.append(str(kwargs["machine_id"]))
        return str(real_validate(*args, **kwargs))

    monkeypatch.setattr("gobby.utils.checkout_root.validate_checkout_root", _wrap)
    project = LocalProjectManager(temp_db).create(
        name="created-none",
        repo_path=str(root),
        machine_id=None,
    )
    assert project.id == project_id
    assert validate_calls == [machine_id]
    checkout = LocalProjectCheckoutManager(temp_db).get(machine_id, project.id)
    assert checkout is not None
    assert checkout.root_path == str(root)


def test_create_uses_matching_provided_machine_id(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    root = tmp_path / "created-local"
    root.mkdir()
    project_id = str(uuid.uuid4())
    write_project_marker(root, project_id=project_id, name="created-local")
    monkeypatch.setattr("gobby.storage.projects.uuid.uuid4", lambda: uuid.UUID(project_id))
    project = LocalProjectManager(temp_db).create(
        name="created-local",
        repo_path=str(root),
        machine_id=machine_id,
    )
    assert project.id == project_id


def test_create_rejects_foreign_machine_before_filesystem(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    foreign = str(uuid.uuid4())
    fs_calls: list[str] = []

    def _boom(*_args: Any, **_kwargs: Any) -> str:
        fs_calls.append("validate")
        raise AssertionError("filesystem must not run for a foreign machine")

    monkeypatch.setattr("gobby.utils.checkout_root.validate_checkout_root", _boom)
    with pytest.raises(MachineOwnershipMismatchError):
        LocalProjectManager(temp_db).create(
            name="created-foreign",
            repo_path="~/not-expanded",
            machine_id=foreign,
        )
    assert fs_calls == []
