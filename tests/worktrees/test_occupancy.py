"""The live-session guard every managed worktree delete runs (#23631)."""

from pathlib import Path

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.worktrees.occupancy import refuse_occupied_worktree
from tests.fixtures.isolated_checkout import (
    install_isolated_checkout_project,
    patch_local_machine_id,
)

pytestmark = pytest.mark.unit
MACHINE_ID = "21000000-0000-4000-8000-000000023631"


@pytest.fixture(autouse=True)
def _local_machine_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, MACHINE_ID)


@pytest.fixture
def project_id(temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "checkout", machine_id=MACHINE_ID, monkeypatch=monkeypatch
    )
    return str(isolated.project.id)


def _live_session(db: HubDatabase, project_id: str, name: str, cwd: Path | None) -> Session:
    manager = SessionManager(db)
    registered = manager.register(
        external_id=f"occupancy-{name}",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=project_id,
        workspace_path=str(cwd) if cwd is not None else None,
    )
    session = manager.get(registered.id)
    assert session is not None
    return session


def _refusal(refs: list[str], worktree: Path | str) -> str:
    noun = "session" if len(refs) == 1 else "sessions"
    return f"Live {noun} {', '.join(sorted(refs))} working in {worktree}; it was not deleted"


@pytest.mark.parametrize("cwd", ["wt", "wt/src/deep"])
def test_refuses_while_a_live_session_works_inside(
    temp_db: HubDatabase, project_id: str, tmp_path: Path, cwd: str
) -> None:
    worktree = tmp_path / "wt"
    session = _live_session(temp_db, project_id, "inside", tmp_path / cwd)

    assert refuse_occupied_worktree(temp_db, str(worktree)) == _refusal([session.ref], worktree)


@pytest.mark.parametrize("cwd", ["wt-sibling", "elsewhere"])
def test_allows_a_session_working_outside(
    temp_db: HubDatabase, project_id: str, tmp_path: Path, cwd: str
) -> None:
    _live_session(temp_db, project_id, "outside", tmp_path / cwd)

    assert refuse_occupied_worktree(temp_db, str(tmp_path / "wt")) is None


def test_refuses_while_the_bound_session_is_live_anywhere(
    temp_db: HubDatabase, project_id: str, tmp_path: Path
) -> None:
    owner = _live_session(temp_db, project_id, "owner", None)
    worktree = LocalWorktreeManager(temp_db).create(
        project_id=project_id,
        branch_name="feature/occupied",
        worktree_path=str(tmp_path / "wt"),
        agent_session_id=owner.id,
    )

    assert refuse_occupied_worktree(
        temp_db, worktree.worktree_path, worktree_id=worktree.id
    ) == _refusal([owner.ref], worktree.worktree_path)
    assert refuse_occupied_worktree(temp_db, worktree.worktree_path) is None


def test_an_ended_session_inside_does_not_block(
    temp_db: HubDatabase, project_id: str, tmp_path: Path
) -> None:
    worktree = tmp_path / "wt"
    session = _live_session(temp_db, project_id, "ended", worktree)
    SessionManager(temp_db).update_status(session.id, "completed")

    assert refuse_occupied_worktree(temp_db, str(worktree)) is None


def test_names_every_occupant_except_the_requester(
    temp_db: HubDatabase, project_id: str, tmp_path: Path
) -> None:
    worktree = tmp_path / "wt"
    requester = _live_session(temp_db, project_id, "requester", worktree)
    other = _live_session(temp_db, project_id, "other", worktree / "sub")

    assert refuse_occupied_worktree(temp_db, str(worktree)) == _refusal(
        [requester.ref, other.ref], worktree
    )
    assert refuse_occupied_worktree(
        temp_db, str(worktree), exclude_session_id=requester.id
    ) == _refusal([other.ref], worktree)
