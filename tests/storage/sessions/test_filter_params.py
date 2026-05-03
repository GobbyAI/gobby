"""Server-side filter predicates on the session list query.

Each test exercises one predicate by registering a small fixture set, then
asserting the filter narrows the result deterministically. Predicates are
combined via AND in production, so per-predicate tests are sufficient — the
combination behavior falls out of basic SQL semantics.
"""

from __future__ import annotations

import pytest

from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

NOW_ISO = "2026-04-29T10:00:00+00:00"


def _register(
    session_manager: SessionManager,
    sample_project: dict,
    *,
    external_id: str,
    source: str = "claude",
    model: str | None = None,
    seq_num: int | None = None,
    agent_depth: int = 0,
    parent_session_id: str | None = None,
    created_at: str = NOW_ISO,
    updated_at: str = NOW_ISO,
) -> str:
    session = session_manager.register(
        external_id=external_id,
        machine_id="machine-abc",
        source=source,
        project_id=sample_project["id"],
    )
    # Patch fields the register helper does not expose.
    session_manager.db.execute(
        """UPDATE sessions
           SET model = ?, seq_num = ?, agent_depth = ?, parent_session_id = ?,
               created_at = ?, updated_at = ?
           WHERE id = ?""",
        (
            model,
            seq_num,
            agent_depth,
            parent_session_id,
            created_at,
            updated_at,
            session.id,
        ),
    )
    return session.id


def _insert_task(
    session_manager: SessionManager,
    sample_project: dict,
    *,
    task_id: str,
    seq_num: int,
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


class TestSourcesFilter:
    def test_sources_in_clause(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        _register(session_manager, sample_project, external_id="a", source="claude")
        _register(session_manager, sample_project, external_id="b", source="codex")
        _register(session_manager, sample_project, external_id="c", source="gemini")

        results = session_manager.list(
            project_id=sample_project["id"],
            sources=["claude", "codex"],
        )
        kept_sources = sorted(s.source for s in results)

        assert kept_sources == ["claude", "codex"]


class TestStatusesFilter:
    def _set_status(
        self,
        session_manager: SessionManager,
        session_id: str,
        status: str,
    ) -> None:
        session_manager.db.execute(
            "UPDATE sessions SET status = ? WHERE id = ?",
            (status, session_id),
        )

    def test_statuses_in_clause_narrows_to_live(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        sid_active = _register(session_manager, sample_project, external_id="a")
        sid_paused = _register(session_manager, sample_project, external_id="b")
        sid_expired = _register(session_manager, sample_project, external_id="c")
        self._set_status(session_manager, sid_paused, "paused")
        self._set_status(session_manager, sid_expired, "expired")

        results = session_manager.list(
            project_id=sample_project["id"],
            statuses=["active", "paused"],
        )
        kept_ids = {s.id for s in results}

        assert kept_ids == {sid_active, sid_paused}

    def test_statuses_in_clause_narrows_to_expired(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        _register(session_manager, sample_project, external_id="a")
        sid_expired = _register(session_manager, sample_project, external_id="b")
        self._set_status(session_manager, sid_expired, "expired")

        results = session_manager.list(
            project_id=sample_project["id"],
            statuses=["expired"],
        )
        kept_ids = {s.id for s in results}

        assert kept_ids == {sid_expired}

    def test_statuses_excludes_deleted_even_when_listed(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        # The base predicate (status != 'deleted') stays in force; passing
        # 'deleted' inside statuses must not bypass it.
        sid_active = _register(session_manager, sample_project, external_id="a")
        sid_deleted = _register(session_manager, sample_project, external_id="b")
        self._set_status(session_manager, sid_deleted, "deleted")

        results = session_manager.list(
            project_id=sample_project["id"],
            statuses=["active", "deleted"],
        )
        kept_ids = {s.id for s in results}

        assert kept_ids == {sid_active}


class TestModeFilter:
    def test_mode_interactive_keeps_only_depth_zero(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        # parent_session_id must be set so the registered session inherits agent_depth.
        # We patch agent_depth directly post-register since register() always inserts 0.
        sid_human = _register(session_manager, sample_project, external_id="human", agent_depth=0)
        _register(session_manager, sample_project, external_id="auto", agent_depth=2)

        results = session_manager.list(project_id=sample_project["id"], modes=["interactive"])

        kept_ids = {s.id for s in results}
        assert sid_human in kept_ids
        # Auto session is filtered out
        assert len(kept_ids) == 1

    def test_mode_auto_keeps_only_depth_one_or_more(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        _register(session_manager, sample_project, external_id="human", agent_depth=0)
        sid_auto = _register(session_manager, sample_project, external_id="auto", agent_depth=1)

        results = session_manager.list(project_id=sample_project["id"], modes=["auto"])

        kept_ids = {s.id for s in results}
        assert sid_auto in kept_ids
        assert len(kept_ids) == 1

    def test_mode_both_selected_is_no_filter(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        _register(session_manager, sample_project, external_id="human", agent_depth=0)
        _register(session_manager, sample_project, external_id="auto", agent_depth=1)

        results = session_manager.list(
            project_id=sample_project["id"],
            modes=["interactive", "auto"],
        )

        assert len(results) == 2


class TestModelFilter:
    def test_models_in_clause(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        _register(session_manager, sample_project, external_id="a", model="claude-opus-4-7")
        _register(session_manager, sample_project, external_id="b", model="claude-sonnet-4-6")
        _register(session_manager, sample_project, external_id="c", model="gpt-5")

        results = session_manager.list(
            project_id=sample_project["id"],
            models=["claude-opus-4-7", "gpt-5"],
        )
        kept_models = sorted(s.model or "" for s in results)

        assert kept_models == ["claude-opus-4-7", "gpt-5"]


class TestSessionSeqRangeFilter:
    def test_session_seq_min_max(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        for index in range(1, 6):
            _register(
                session_manager,
                sample_project,
                external_id=f"s{index}",
                seq_num=index * 10,
            )

        results = session_manager.list(
            project_id=sample_project["id"],
            session_seq_min=20,
            session_seq_max=40,
        )
        kept_seq = sorted(s.seq_num or 0 for s in results)

        assert kept_seq == [20, 30, 40]


class TestTaskRefFilter:
    def test_default_role_is_claimed_when_range_set_without_roles(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        sid_match = _register(session_manager, sample_project, external_id="claimed-it")
        sid_other = _register(session_manager, sample_project, external_id="created-it")
        _insert_task(
            session_manager, sample_project, task_id="t1", seq_num=42, claimed_by=sid_match
        )
        _insert_task(
            session_manager, sample_project, task_id="t2", seq_num=43, created_in=sid_other
        )

        results = session_manager.list(
            project_id=sample_project["id"],
            task_ref_min=40,
            task_ref_max=50,
        )
        kept_ids = {s.id for s in results}

        assert sid_match in kept_ids
        assert sid_other not in kept_ids

    def test_multi_role_or_match(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        sid_claimed = _register(session_manager, sample_project, external_id="claimed")
        sid_created = _register(session_manager, sample_project, external_id="created")
        sid_closed = _register(session_manager, sample_project, external_id="closed")
        sid_unrelated = _register(session_manager, sample_project, external_id="unrelated")

        _insert_task(
            session_manager, sample_project, task_id="t-c", seq_num=10, claimed_by=sid_claimed
        )
        _insert_task(
            session_manager, sample_project, task_id="t-r", seq_num=20, created_in=sid_created
        )
        _insert_task(
            session_manager, sample_project, task_id="t-l", seq_num=30, closed_in=sid_closed
        )
        # Unrelated session has a task linked but outside the seq range
        _insert_task(
            session_manager,
            sample_project,
            task_id="t-out",
            seq_num=999,
            claimed_by=sid_unrelated,
        )

        results = session_manager.list(
            project_id=sample_project["id"],
            task_ref_min=1,
            task_ref_max=100,
            task_ref_roles=["claimed", "created", "closed"],
        )
        kept_ids = {s.id for s in results}

        assert sid_claimed in kept_ids
        assert sid_created in kept_ids
        assert sid_closed in kept_ids
        assert sid_unrelated not in kept_ids

    def test_open_lower_bound(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        sid_low = _register(session_manager, sample_project, external_id="low")
        sid_high = _register(session_manager, sample_project, external_id="high")
        _insert_task(
            session_manager, sample_project, task_id="t-low", seq_num=5, claimed_by=sid_low
        )
        _insert_task(
            session_manager, sample_project, task_id="t-high", seq_num=500, claimed_by=sid_high
        )

        results = session_manager.list(
            project_id=sample_project["id"],
            task_ref_max=100,
        )
        kept_ids = {s.id for s in results}

        assert sid_low in kept_ids
        assert sid_high not in kept_ids


class TestCreatedAtRangeFilter:
    def test_created_after_inclusive(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        sid_old = _register(
            session_manager,
            sample_project,
            external_id="old",
            created_at="2026-01-01T00:00:00+00:00",
        )
        sid_new = _register(
            session_manager,
            sample_project,
            external_id="new",
            created_at="2026-04-01T00:00:00+00:00",
        )

        results = session_manager.list(
            project_id=sample_project["id"],
            created_after="2026-03-01T00:00:00+00:00",
        )
        kept_ids = {s.id for s in results}

        assert sid_new in kept_ids
        assert sid_old not in kept_ids

    def test_created_before_exclusive(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        cutoff = "2026-03-01T00:00:00+00:00"
        sid_before = _register(
            session_manager,
            sample_project,
            external_id="before",
            created_at="2026-02-15T00:00:00+00:00",
        )
        sid_at = _register(
            session_manager,
            sample_project,
            external_id="at",
            created_at=cutoff,
        )

        results = session_manager.list(project_id=sample_project["id"], created_before=cutoff)
        kept_ids = {s.id for s in results}

        assert sid_before in kept_ids
        # The cutoff itself is excluded
        assert sid_at not in kept_ids
