"""A paused seat whose pane and run are gone is resumable; a live one is refused (#24650)."""

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.spawn_agent import _resume as resume
from gobby.storage.session_models import Session

pytestmark = pytest.mark.unit


@pytest.fixture
def stored_agent_type() -> Iterator[MagicMock]:
    with patch.object(resume, "SessionVariableManager") as manager:
        manager.return_value.get_variable_subset.return_value = {"_agent_type": "developer"}
        yield manager.return_value.get_variable_subset


def _seat(*, status: str, run_id: str | None) -> Session:
    return MagicMock(
        spec=Session,
        id="seat-session",
        project_id="project",
        source="codex",
        external_id="recorded-thread",
        status=status,
        agent_run_id=run_id,
    )


def _sessions(target: Session, pane_rows: list[tuple[int]]) -> MagicMock:
    sessions = MagicMock()
    sessions.resolve_session_reference.return_value = target.id
    sessions.get.return_value = target
    sessions.db.fetchall.return_value = pane_rows
    return sessions


def _run_storage(run_status: str | None) -> MagicMock:
    storage = MagicMock()
    storage.get.return_value = None if run_status is None else SimpleNamespace(status=run_status)
    return storage


@pytest.mark.parametrize(
    ("run_id", "run_status"),
    [
        (None, None),
        ("run-1", "cancelled"),
        ("run-1", "failed"),
    ],
    ids=["absent-run", "cancelled-run", "failed-run"],
)
def test_paused_seat_without_live_pane_or_active_run_resumes(
    stored_agent_type: MagicMock, run_id: str | None, run_status: str | None
) -> None:
    target = _seat(status="paused", run_id=run_id)

    resolved = resume.resolve_resume_target(
        _sessions(target, pane_rows=[]),
        "gobby#123",
        "project",
        None,
        _run_storage(run_status),
        "developer",
    )

    assert resolved.id == target.id


@pytest.mark.parametrize(
    ("run_id", "run_status", "pane_rows"),
    [
        ("run-1", "cancelled", [(1,)]),
        ("run-1", "running", []),
    ],
    ids=["live-pane", "active-run"],
)
def test_paused_seat_with_live_pane_or_active_run_stays_refused(
    stored_agent_type: MagicMock,
    run_id: str,
    run_status: str,
    pane_rows: list[tuple[int]],
) -> None:
    target = _seat(status="paused", run_id=run_id)

    with pytest.raises(ValueError, match=r"is still live \(paused\)"):
        resume.resolve_resume_target(
            _sessions(target, pane_rows=pane_rows),
            "gobby#123",
            "project",
            None,
            _run_storage(run_status),
            "developer",
        )


def test_non_paused_live_seat_stays_refused_without_a_pane(stored_agent_type: MagicMock) -> None:
    target = _seat(status="active", run_id=None)

    with pytest.raises(ValueError, match=r"is still live \(active\)"):
        resume.resolve_resume_target(
            _sessions(target, pane_rows=[]),
            "gobby#123",
            "project",
            None,
            _run_storage(None),
            "developer",
        )


def test_resumed_seat_inherits_the_lane_its_last_run_recorded() -> None:
    storage = MagicMock()
    storage.get.return_value = SimpleNamespace(resume_metadata_json={"lane": "7"})

    assert resume.resumed_seat_lane(_seat(status="paused", run_id="run-1"), storage) == "7"


def test_resumed_seat_without_a_run_has_no_lane() -> None:
    assert resume.resumed_seat_lane(_seat(status="paused", run_id=None), MagicMock()) is None
