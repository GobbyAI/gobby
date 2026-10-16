"""Operator approvals for unowned dirty paths: recording, expiry and who may write them.

Also covers the edit ledgers the gate trusts, which no seat may write either.
"""

from __future__ import annotations

import getpass
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gobby.cli.sessions import sessions
from gobby.mcp_proxy.tools.workflows._variables import set_variable
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.workflows.dirty_edit_grants import (
    OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE,
    approved_dirty_paths,
    record_dirty_path_approval,
)
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def seat(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase, tmp_path: Path
) -> Session:
    project = isolated_checkout_factory(temp_db, "dirty-approval-test", root=tmp_path).project
    return SessionManager(temp_db).register(
        external_id="seat-external",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project.id,
    )


def _approve(db: HubDatabase, seat: Session, root: Path, paths: list[str]) -> set[str]:
    record_dirty_path_approval(
        db, seat.id, checkout_root=str(root), paths=paths, approved_by="operator"
    )
    return approved_dirty_paths(SessionVariableManager(db).get_variables(seat.id), str(root))


def test_dirty_path_approval_records_exact_checkout_relative_paths(
    temp_db: HubDatabase, seat: Session, tmp_path: Path
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("orphaned\n", encoding="utf-8")

    approved = _approve(temp_db, seat, tmp_path, ["src/a.py", str(tmp_path / "b.txt")])
    variables = SessionVariableManager(temp_db).get_variables(seat.id)

    assert approved == {"src/a.py", "b.txt"}
    assert approved_dirty_paths(variables, str(tmp_path / "src")) == set()
    for outside in ("../escape.txt", str(tmp_path.parent / "escape.txt"), ".", "", "src"):
        with pytest.raises(ValueError):
            record_dirty_path_approval(
                temp_db,
                seat.id,
                checkout_root=str(tmp_path),
                paths=[outside],
                approved_by="operator",
            )


def test_dirty_path_approval_lapses_when_path_content_changes(
    temp_db: HubDatabase, seat: Session, tmp_path: Path
) -> None:
    (tmp_path / "edited.txt").write_text("approved content\n", encoding="utf-8")
    (tmp_path / "removed.txt").write_text("approved content\n", encoding="utf-8")

    approved = _approve(temp_db, seat, tmp_path, ["edited.txt", "removed.txt", "created.txt"])
    (tmp_path / "edited.txt").write_text("changed after approval\n", encoding="utf-8")
    (tmp_path / "removed.txt").unlink()
    (tmp_path / "created.txt").write_text("appeared after approval\n", encoding="utf-8")
    variables = SessionVariableManager(temp_db).get_variables(seat.id)

    assert approved == {"edited.txt", "removed.txt", "created.txt"}
    assert approved_dirty_paths(variables, str(tmp_path)) == set()


@pytest.mark.parametrize("scope", ["session", "step"])
def test_seat_variable_writes_cannot_create_a_dirty_path_approval(
    temp_db: HubDatabase, seat: Session, tmp_path: Path, scope: Literal["session", "step"]
) -> None:
    forged = {str(tmp_path): {"paths": ["a.txt"], "approved_by": "seat", "approved_at": "now"}}

    result = set_variable(
        SessionManager(temp_db),
        temp_db,
        name=OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE,
        value=forged,
        session_id=seat.id,
        scope=scope,
    )
    variables = SessionVariableManager(temp_db).get_variables(seat.id)

    assert result["success"] is False
    assert OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE not in variables


@pytest.mark.parametrize(
    ("name", "forged"),
    [
        ("session_dirty_files", ["a.txt"]),
        ("session_dirty_file_checkouts", {"/repo": ["a.txt"]}),
        ("task_edited_file_checkouts", {"task-id": {"/repo": ["a.txt"]}}),
    ],
)
def test_seat_variable_writes_cannot_forge_the_edit_ledgers_the_gate_trusts(
    temp_db: HubDatabase, seat: Session, name: str, forged: list[str] | dict[str, Any]
) -> None:
    result = set_variable(
        SessionManager(temp_db), temp_db, name=name, value=forged, session_id=seat.id
    )
    variables = SessionVariableManager(temp_db).get_variables(seat.id)

    assert result["success"] is False
    assert variables.get(name) != forged


def test_approve_dirty_paths_cli_records_the_operator_approval(
    temp_db: HubDatabase, seat: Session, tmp_path: Path
) -> None:
    with (
        patch("gobby.cli.sessions_dirty_paths.require_cli_database", return_value=temp_db),
        patch("gobby.cli.sessions_dirty_paths.resolve_session_id", side_effect=lambda ref: ref),
    ):
        result = CliRunner().invoke(
            sessions,
            ["approve-dirty-paths", seat.id, "--checkout", str(tmp_path), "a.txt", "src/b.py"],
        )
        refused = CliRunner().invoke(
            sessions,
            ["approve-dirty-paths", seat.id, "--checkout", str(tmp_path), "../c.txt"],
        )
    variables = SessionVariableManager(temp_db).get_variables(seat.id)
    stored = variables[OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE][str(tmp_path.resolve())]

    assert result.exit_code == 0, result.output
    assert approved_dirty_paths(variables, str(tmp_path)) == {"a.txt", "src/b.py"}
    assert stored["approved_by"] == getpass.getuser()
    assert refused.exit_code != 0
    assert "../c.txt" in refused.output
