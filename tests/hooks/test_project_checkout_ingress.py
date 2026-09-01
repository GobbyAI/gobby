"""Hook ingress checkout registration fails soft on same-machine refusals."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from gobby.hooks.project_checkout_ingress import register_cwd_marker_checkout
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.projects import LocalProjectManager
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    insert_overlay,
    patch_local_machine_id,
    write_project_marker,
)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

_LOGGER_NAME = "tests.hooks.project_checkout_ingress"


def _context(root: Path, project_id: str, name: str) -> dict[str, Any]:
    return {"id": project_id, "name": name, "project_path": str(root)}


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


@pytest.fixture
def ingress_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


def test_second_clone_conflict_warns_once_and_keeps_hook_alive(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ingress_logger: logging.Logger,
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="ingress-conflict")
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        root.mkdir()
        write_project_marker(root, project_id=project.id, name=project.name)
    LocalProjectCheckoutManager(temp_db).register(machine_id, project.id, str(first))

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        register_cwd_marker_checkout(
            temp_db, _context(second, project.id, project.name), logger=ingress_logger
        )

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert f"gobby projects rebind {project.name} {second}" in warnings[0]
    checkout = LocalProjectCheckoutManager(temp_db).get(machine_id, project.id)
    assert checkout is not None
    assert checkout.root_path == str(first)


def test_root_owned_by_another_project_warns_and_continues(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ingress_logger: logging.Logger,
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    manager = LocalProjectManager(temp_db)
    owner = manager.create(name="ingress-owner")
    newcomer = manager.create(name="ingress-newcomer")
    root = tmp_path / "shared"
    root.mkdir()
    write_project_marker(root, project_id=newcomer.id, name=newcomer.name)
    LocalProjectCheckoutManager(temp_db).register(machine_id, owner.id, str(root))

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        register_cwd_marker_checkout(
            temp_db, _context(root, newcomer.id, newcomer.name), logger=ingress_logger
        )

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert f"gobby projects rebind {newcomer.name} {root}" in warnings[0]
    assert LocalProjectCheckoutManager(temp_db).get(machine_id, newcomer.id) is None


def test_overlay_cwd_leaves_tracked_marker_untouched(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ingress_logger: logging.Logger,
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="ingress-overlay")
    overlay = tmp_path / "worktree"
    overlay.mkdir()
    write_project_marker(overlay, project_id=project.id, name="stale-name")
    insert_overlay(
        temp_db,
        project_id=project.id,
        machine_id=machine_id,
        path=str(overlay),
        kind="worktree",
    )
    marker = overlay / ".gobby" / "project.json"
    before = marker.read_text(encoding="utf-8")

    register_cwd_marker_checkout(
        temp_db, _context(overlay, project.id, "stale-name"), logger=ingress_logger
    )

    assert marker.read_text(encoding="utf-8") == before
    assert LocalProjectCheckoutManager(temp_db).get(machine_id, project.id) is None


def test_marker_refresh_oserror_is_logged_not_raised(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ingress_logger: logging.Logger,
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="ingress-readonly")
    root = tmp_path / "repo"
    root.mkdir()
    write_project_marker(root, project_id=project.id, name="stale-name")

    def _read_only(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("read-only tree")

    monkeypatch.setattr("gobby.utils.project_init.refresh_marker_expected_id", _read_only)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        register_cwd_marker_checkout(
            temp_db, _context(root, project.id, "stale-name"), logger=ingress_logger
        )

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "read-only tree" in warnings[0]
    checkout = LocalProjectCheckoutManager(temp_db).get(machine_id, project.id)
    assert checkout is not None
    assert checkout.root_path == str(root)
