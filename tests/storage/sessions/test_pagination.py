"""Cursor pagination over the session list query."""

from __future__ import annotations

import pytest

from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


def _register(
    session_manager: SessionManager,
    sample_project: dict,
    *,
    external_id: str,
    updated_at: str,
) -> str:
    session = session_manager.register(
        external_id=external_id,
        machine_id="machine-abc",
        source="claude",
        project_id=sample_project["id"],
    )
    session_manager.db.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        (updated_at, session.id),
    )
    return session.id


class TestSessionCursorPagination:
    """The list() query supports compound-cursor pagination on (updated_at, id) DESC."""

    def test_no_cursor_returns_full_window(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        for index in range(5):
            _register(
                session_manager,
                sample_project,
                external_id=f"session-{index}",
                updated_at=f"2026-04-29T10:00:{index:02d}+00:00",
            )

        results = session_manager.list(project_id=sample_project["id"], limit=10)

        assert len(results) == 5
        # DESC by updated_at
        timestamps = [s.updated_at for s in results]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_cursor_returns_strictly_older_rows(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        ids = [
            _register(
                session_manager,
                sample_project,
                external_id=f"session-{index}",
                updated_at=f"2026-04-29T10:00:{index:02d}+00:00",
            )
            for index in range(5)
        ]

        page_one = session_manager.list(project_id=sample_project["id"], limit=2)
        assert len(page_one) == 2

        page_two = session_manager.list(
            project_id=sample_project["id"],
            limit=2,
            cursor_updated_at=page_one[-1].updated_at,
            cursor_id=page_one[-1].id,
        )

        assert len(page_two) == 2
        seen_ids = {s.id for s in page_one} | {s.id for s in page_two}
        assert len(seen_ids) == 4
        # Cursor row itself never reappears
        assert page_one[-1].id not in {s.id for s in page_two}
        # Both pages stay within the registered set
        assert seen_ids.issubset(set(ids))

    def test_cursor_breaks_ties_on_id(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        # Three sessions sharing the same updated_at — id must break the tie
        shared_ts = "2026-04-29T10:00:00+00:00"
        ids = [
            _register(
                session_manager,
                sample_project,
                external_id=f"tie-{index}",
                updated_at=shared_ts,
            )
            for index in range(3)
        ]

        page_one = session_manager.list(project_id=sample_project["id"], limit=2)
        assert len(page_one) == 2

        page_two = session_manager.list(
            project_id=sample_project["id"],
            limit=2,
            cursor_updated_at=page_one[-1].updated_at,
            cursor_id=page_one[-1].id,
        )

        assert len(page_two) == 1
        assert page_two[0].id != page_one[0].id
        assert page_two[0].id != page_one[1].id
        assert page_two[0].id in ids

    def test_partial_cursor_is_ignored(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        # Either cursor param without the other reverts to "no cursor" semantics —
        # callers should not be able to half-apply a cursor by accident.
        for index in range(3):
            _register(
                session_manager,
                sample_project,
                external_id=f"session-{index}",
                updated_at=f"2026-04-29T10:00:{index:02d}+00:00",
            )

        results_partial_ts = session_manager.list(
            project_id=sample_project["id"],
            limit=10,
            cursor_updated_at="2026-04-29T10:00:00+00:00",
        )
        assert len(results_partial_ts) == 3

        results_partial_id = session_manager.list(
            project_id=sample_project["id"],
            limit=10,
            cursor_id="some-id",
        )
        assert len(results_partial_id) == 3
