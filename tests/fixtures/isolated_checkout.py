"""Isolated-machine project, marker, and checkout setup for tests."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.projects import LocalProjectManager, Project
from tests.fixtures.postgres import TEST_USER_ID


@dataclass(frozen=True)
class IsolatedCheckoutProject:
    """One isolated machine, project row, marker, and checkout."""

    machine_id: str
    project: Project
    root_path: str


def write_project_marker(root: Path, *, project_id: str, name: str) -> None:
    """Write `.gobby/project.json` at `root` for `project_id`."""
    gobby_dir = root / ".gobby"
    gobby_dir.mkdir(parents=True, exist_ok=True)
    (gobby_dir / "project.json").write_text(
        json.dumps({"id": project_id, "name": name}),
        encoding="utf-8",
    )


def insert_isolated_machine(db: HubDatabase, machine_id: str | None = None) -> str:
    """Insert a machines row and return its id."""
    resolved = machine_id or str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO machines (id, hostname, owner_user_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (resolved, f"host-{resolved}", TEST_USER_ID),
    )
    return resolved


def patch_local_machine_id(monkeypatch: Any, machine_id: str) -> None:
    """Pin the machine-id cache and both imported `require_machine_id` names.

    Production modules bind `require_machine_id` by direct import (for example
    `gobby.agents.launcher_session`), so the cache pin is what keeps every binding
    agreeing with the explicitly patched storage and utils names.
    """
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", machine_id)
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_machine_id",
        lambda: machine_id,
    )
    monkeypatch.setattr(
        "gobby.utils.machine_id.require_machine_id",
        lambda: machine_id,
    )


def insert_overlay(
    db: HubDatabase,
    *,
    project_id: str,
    machine_id: str,
    path: str,
    kind: str,
) -> None:
    """Insert a worktree or clone overlay row."""
    overlay_id = str(uuid.uuid4())
    if kind == "worktree":
        db.execute(
            """
            INSERT INTO worktrees (
                id, project_id, machine_id, branch_name, worktree_path
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (overlay_id, project_id, machine_id, "task/overlay", path),
        )
        return
    if kind == "clone":
        db.execute(
            """
            INSERT INTO clones (
                id, project_id, machine_id, branch_name, clone_path
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (overlay_id, project_id, machine_id, "task/clone", path),
        )
        return
    raise ValueError(f"unknown overlay kind {kind}")


def install_isolated_checkout_project(
    db: HubDatabase,
    root: Path,
    *,
    name: str = "test-project",
    github_url: str | None = "https://github.com/test/test-project",
    machine_id: str | None = None,
    monkeypatch: Any | None = None,
) -> IsolatedCheckoutProject:
    """Create an isolated machine, marker, project, and checkout."""
    resolved_machine_id = insert_isolated_machine(db, machine_id)
    if monkeypatch is not None:
        patch_local_machine_id(monkeypatch, resolved_machine_id)
    root.mkdir(parents=True, exist_ok=True)
    project = LocalProjectManager(db).create(name=name, github_url=github_url)
    write_project_marker(root, project_id=project.id, name=name)
    root_path = str(root)
    LocalProjectCheckoutManager(db).register(resolved_machine_id, project.id, root_path)
    return IsolatedCheckoutProject(
        machine_id=resolved_machine_id,
        project=project,
        root_path=root_path,
    )
