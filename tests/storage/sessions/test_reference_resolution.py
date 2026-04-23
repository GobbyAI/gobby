"""Focused tests for session storage behavior."""

import pytest

from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class TestSessionManagerReferenceResolution:
    """Tests split from the SessionManager storage monolith."""

    def test_find_parent_no_handoff_ready(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test find_parent returns None when no handoff_ready session."""
        # Create an active session (not handoff_ready)
        session_manager.register(
            external_id="active-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        result = session_manager.find_parent(
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        assert result is None

    def test_find_parent_without_source_filter(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test find_parent without source filter finds any source."""
        session = session_manager.register(
            external_id="parent-any",
            machine_id="machine-1",
            source="gemini",
            project_id=sample_project["id"],
        )
        session_manager.update_status(session.id, "handoff_ready")

        # Find without source filter
        found = session_manager.find_parent(
            machine_id="machine-1",
            project_id=sample_project["id"],
            source=None,  # No source filter
        )

        assert found is not None
        assert found.id == session.id


class TestProjectScopedSeqNum:
    """Tests for project-scoped session seq_num feature."""

    def test_seq_num_per_project(
        self,
        session_manager: SessionManager,
        temp_db,
    ) -> None:
        """Test that seq_num is assigned per project, not globally."""
        from gobby.storage.projects import LocalProjectManager

        proj_manager = LocalProjectManager(temp_db)
        project1 = proj_manager.create(name="project1", repo_path="/tmp/p1")
        project2 = proj_manager.create(name="project2", repo_path="/tmp/p2")

        # Create sessions in project1
        s1_p1 = session_manager.register(
            external_id="s1-p1", machine_id="m1", source="claude", project_id=project1.id
        )
        s2_p1 = session_manager.register(
            external_id="s2-p1", machine_id="m1", source="claude", project_id=project1.id
        )

        # Create sessions in project2
        s1_p2 = session_manager.register(
            external_id="s1-p2", machine_id="m1", source="claude", project_id=project2.id
        )

        # Project1 sessions should have seq_num 1 and 2
        assert s1_p1.seq_num == 1
        assert s2_p1.seq_num == 2

        # Project2 session should have seq_num 1 (independent from project1)
        assert s1_p2.seq_num == 1

    def test_resolve_session_reference_with_project_id(
        self,
        session_manager: SessionManager,
        temp_db,
    ) -> None:
        """Test resolving #N format with project_id parameter."""
        from gobby.storage.projects import LocalProjectManager

        proj_manager = LocalProjectManager(temp_db)
        project1 = proj_manager.create(name="proj1", repo_path="/tmp/proj1")
        project2 = proj_manager.create(name="proj2", repo_path="/tmp/proj2")

        # Create #1 in each project
        s1 = session_manager.register(
            external_id="s1", machine_id="m1", source="claude", project_id=project1.id
        )
        s2 = session_manager.register(
            external_id="s2", machine_id="m1", source="claude", project_id=project2.id
        )

        # Resolve #1 with project1 context
        resolved1 = session_manager.resolve_session_reference("#1", project_id=project1.id)
        assert resolved1 == s1.id

        # Resolve #1 with project2 context
        resolved2 = session_manager.resolve_session_reference("#1", project_id=project2.id)
        assert resolved2 == s2.id

    def test_resolve_session_reference_requires_project_id_for_seq_num(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test that resolve_session_reference raises ValueError for #N without project_id."""
        session = session_manager.register(
            external_id="global-test",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )

        # Without project_id, #N resolution must fail (seq_num is per-project)
        with pytest.raises(ValueError, match="project context is required"):
            session_manager.resolve_session_reference(f"#{session.seq_num}")

    def test_resolve_session_reference_uuid_format(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test that UUID format still works with project-scoped resolution."""
        session = session_manager.register(
            external_id="uuid-test",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )

        # UUID should resolve regardless of project_id
        resolved = session_manager.resolve_session_reference(session.id)
        assert resolved == session.id

    def test_resolve_session_reference_not_found(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test ValueError raised when session not found."""
        with pytest.raises(ValueError, match="not found"):
            session_manager.resolve_session_reference("#999", project_id=sample_project["id"])


class TestResolveReferenceExternalId:
    """external_id fallback resolution (Change 1)."""

    def _seed_session_with_external_uuid(
        self,
        session_manager: SessionManager,
        project_id: str,
        *,
        external_id: str,
        source: str = "claude",
        machine_id: str = "m-ext",
    ) -> Session:
        return session_manager.register(
            external_id=external_id,
            machine_id=machine_id,
            source=source,
            project_id=project_id,
        )

    def test_resolve_reference_by_external_id_full_uuid(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """An external_id UUID resolves to the platform id."""
        import uuid as _uuid

        external_uuid = str(_uuid.uuid4())
        sess = self._seed_session_with_external_uuid(
            session_manager, sample_project["id"], external_id=external_uuid
        )
        resolved = session_manager.resolve_session_reference(
            external_uuid, project_id=sample_project["id"]
        )
        assert resolved == sess.id
        assert resolved != external_uuid

    def test_resolve_reference_by_external_id_full_uuid_no_project_scope(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """External_id UUID resolves with project_id=None too."""
        import uuid as _uuid

        external_uuid = str(_uuid.uuid4())
        sess = self._seed_session_with_external_uuid(
            session_manager, sample_project["id"], external_id=external_uuid
        )
        resolved = session_manager.resolve_session_reference(external_uuid)
        assert resolved == sess.id

    def test_resolve_reference_by_external_id_prefix(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """A prefix of an external_id resolves to the platform id."""
        import uuid as _uuid

        external_uuid = str(_uuid.uuid4())
        sess = self._seed_session_with_external_uuid(
            session_manager, sample_project["id"], external_id=external_uuid
        )
        prefix = external_uuid[:8]
        resolved = session_manager.resolve_session_reference(
            prefix, project_id=sample_project["id"]
        )
        assert resolved == sess.id

    def test_resolve_reference_prefers_id_match_over_external_id_match(
        self,
        session_manager: SessionManager,
        sample_project: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If an id prefix matches, it wins regardless of external_id matches."""
        import uuid as _uuid

        monkeypatch.setattr(
            _uuid,
            "uuid4",
            lambda: _uuid.UUID("88212625-de4a-4fbc-b666-cedcdaa2cfc1"),
        )
        sess = session_manager.register(
            external_id="something-unique",
            machine_id="m",
            source="claude",
            project_id=sample_project["id"],
        )
        prefix = sess.id[:8]
        assert prefix.isdigit()
        resolved = session_manager.resolve_session_reference(
            prefix, project_id=sample_project["id"]
        )
        assert resolved == sess.id

    def test_resolve_reference_ambiguous_external_id_in_project_raises(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Two rows with the same external_id in a project → ValueError."""
        import uuid as _uuid

        external_uuid = str(_uuid.uuid4())
        self._seed_session_with_external_uuid(
            session_manager,
            sample_project["id"],
            external_id=external_uuid,
            source="claude",
            machine_id="m-a",
        )
        self._seed_session_with_external_uuid(
            session_manager,
            sample_project["id"],
            external_id=external_uuid,
            source="codex",
            machine_id="m-a",
        )
        with pytest.raises(ValueError, match="Ambiguous"):
            session_manager.resolve_session_reference(
                external_uuid, project_id=sample_project["id"]
            )

    def test_resolve_reference_external_id_cross_project_no_scope_ambiguous_raises(
        self,
        session_manager: SessionManager,
        temp_db,
    ) -> None:
        """Same external_id across two projects with project_id=None → ValueError.

        Inserts directly via SQL because ``SessionManager.register()`` has
        a cross-project recovery path that overwrites the first row's
        ``project_id`` instead of creating a second row.
        """
        import uuid as _uuid
        from datetime import UTC, datetime

        from gobby.storage.projects import LocalProjectManager

        pm = LocalProjectManager(temp_db)
        p1 = pm.create(name="amb-p1", repo_path="/tmp/amb-p1")
        p2 = pm.create(name="amb-p2", repo_path="/tmp/amb-p2")
        external_uuid = str(_uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        for idx, pid in enumerate((p1.id, p2.id)):
            temp_db.execute(
                """
                INSERT INTO sessions (
                    id, external_id, machine_id, source, project_id,
                    status, created_at, updated_at, seq_num
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    str(_uuid.uuid4()),
                    external_uuid,
                    f"m-{idx}",
                    "claude",
                    pid,
                    now,
                    now,
                    idx + 1,
                ),
            )
        with pytest.raises(ValueError, match="Ambiguous"):
            session_manager.resolve_session_reference(external_uuid)

    def test_resolve_reference_ambiguous_external_id_prefix_raises(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Multiple external_ids sharing a prefix → ValueError."""
        shared_prefix = "deadbeef-0000"
        session_manager.register(
            external_id=f"{shared_prefix}-aaaa-aaaaaaaaaaaa",
            machine_id="m-a",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id=f"{shared_prefix}-bbbb-bbbbbbbbbbbb",
            machine_id="m-b",
            source="claude",
            project_id=sample_project["id"],
        )
        with pytest.raises(ValueError, match="Ambiguous"):
            session_manager.resolve_session_reference(
                shared_prefix, project_id=sample_project["id"]
            )

    def test_resolve_reference_unknown_ref_still_raises(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Unknown UUID → ValueError (not found)."""
        import uuid as _uuid

        with pytest.raises(ValueError, match="not found"):
            session_manager.resolve_session_reference(
                str(_uuid.uuid4()), project_id=sample_project["id"]
            )
