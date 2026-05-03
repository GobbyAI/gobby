"""Bulk task-ref join helper on the session storage layer."""

from __future__ import annotations

import pytest

from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

NOW_ISO = "2026-04-29T10:00:00+00:00"


def _register_session(
    session_manager: SessionManager,
    sample_project: dict,
    *,
    external_id: str,
) -> str:
    return session_manager.register(
        external_id=external_id,
        machine_id="machine-abc",
        source="claude",
        project_id=sample_project["id"],
    ).id


def _insert_task(
    session_manager: SessionManager,
    sample_project: dict,
    *,
    task_id: str,
    seq_num: int | None,
    claimed_by: str | None = None,
    created_in: str | None = None,
    closed_in: str | None = None,
) -> None:
    session_manager.db.execute(
        """
        INSERT INTO tasks (
            id, project_id, title,
            seq_num, claimed_by_session_id, created_in_session_id, closed_in_session_id,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            sample_project["id"],
            f"Task {task_id}",
            seq_num,
            claimed_by,
            created_in,
            closed_in,
            NOW_ISO,
            NOW_ISO,
        ),
    )


class TestFetchTaskRefsBySession:
    """fetch_task_refs_by_session bulk-joins tasks across three role columns."""

    def test_empty_input_returns_empty(
        self,
        session_manager: SessionManager,
    ) -> None:
        assert session_manager.fetch_task_refs_by_session([]) == {}

    def test_session_without_task_refs_returns_empty_buckets(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        sid = _register_session(session_manager, sample_project, external_id="quiet")

        result = session_manager.fetch_task_refs_by_session([sid])

        assert result == {sid: {"claimed": [], "created": [], "closed": []}}

    def test_three_roles_for_one_session(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        # One session that played all three roles on different tasks.
        sid = _register_session(session_manager, sample_project, external_id="busy")
        _insert_task(
            session_manager,
            sample_project,
            task_id="t-claimed",
            seq_num=10,
            claimed_by=sid,
        )
        _insert_task(
            session_manager,
            sample_project,
            task_id="t-created",
            seq_num=20,
            created_in=sid,
        )
        _insert_task(
            session_manager,
            sample_project,
            task_id="t-closed",
            seq_num=30,
            closed_in=sid,
        )

        result = session_manager.fetch_task_refs_by_session([sid])

        assert result[sid]["claimed"] == [10]
        assert result[sid]["created"] == [20]
        assert result[sid]["closed"] == [30]

    def test_one_task_can_carry_multiple_roles(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        # Same session both created and claimed a single task — both lists fire.
        sid = _register_session(session_manager, sample_project, external_id="dual")
        _insert_task(
            session_manager,
            sample_project,
            task_id="t-dual",
            seq_num=42,
            claimed_by=sid,
            created_in=sid,
        )

        result = session_manager.fetch_task_refs_by_session([sid])

        assert result[sid]["claimed"] == [42]
        assert result[sid]["created"] == [42]
        assert result[sid]["closed"] == []

    def test_returns_seq_nums_sorted_ascending(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        # ORDER BY seq_num in the helper means each role list is monotonically
        # increasing — the frontend can rely on it for "earliest task" displays.
        sid = _register_session(session_manager, sample_project, external_id="ordered")
        for index, seq in enumerate([99, 1, 50]):
            _insert_task(
                session_manager,
                sample_project,
                task_id=f"t-{index}",
                seq_num=seq,
                claimed_by=sid,
            )

        result = session_manager.fetch_task_refs_by_session([sid])

        assert result[sid]["claimed"] == [1, 50, 99]

    def test_skips_tasks_with_null_seq_num(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        sid = _register_session(session_manager, sample_project, external_id="legacy")
        _insert_task(
            session_manager,
            sample_project,
            task_id="t-legacy",
            seq_num=None,
            claimed_by=sid,
        )

        result = session_manager.fetch_task_refs_by_session([sid])

        assert result[sid]["claimed"] == []

    def test_unrelated_sessions_get_empty_lists(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        sid_a = _register_session(session_manager, sample_project, external_id="a")
        sid_b = _register_session(session_manager, sample_project, external_id="b")
        _insert_task(
            session_manager,
            sample_project,
            task_id="t-a",
            seq_num=5,
            claimed_by=sid_a,
        )

        result = session_manager.fetch_task_refs_by_session([sid_a, sid_b])

        assert result[sid_a]["claimed"] == [5]
        assert result[sid_b] == {"claimed": [], "created": [], "closed": []}
