"""Focused tests for session storage behavior."""

from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "20000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


class TestSessionManagerReferenceResolution:
    """Tests split from the SessionManager storage monolith."""

    def test_find_parent_no_awaiting_handoff(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test find_parent returns None when no awaiting_handoff session."""
        # Create an active session (not awaiting_handoff)
        session_manager.register(
            external_id="active-session",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=sample_project["id"],
        )

        result = session_manager.find_parent(
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=sample_project["id"],
        )
        assert result is None

    def test_find_parent_without_source_filter(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test find_parent without source filter finds any source."""
        session = session_manager.register(
            external_id="parent-any",
            machine_id=LOCAL_MACHINE_ID,
            source="qwen",
            project_id=sample_project["id"],
        )
        session_manager.update_status(session.id, "awaiting_handoff")

        # Find without source filter
        found = session_manager.find_parent(
            machine_id=LOCAL_MACHINE_ID,
            project_id=sample_project["id"],
            source=None,  # No source filter
        )

        assert found is not None
        assert found.id == session.id


class TestProjectScopedSeqNum:
    """Tests for project-scoped session seq_num feature."""

    def test_seq_num_per_project(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        session_manager: SessionManager,
        temp_db: HubDatabase,
    ) -> None:
        """Test that seq_num is assigned per project, not globally."""
        from gobby.storage.projects import LocalProjectManager

        proj_manager = LocalProjectManager(temp_db)
        project1 = isolated_checkout_factory(proj_manager.db, "project1").project
        project2 = isolated_checkout_factory(proj_manager.db, "project2").project

        # Create sessions in project1
        s1_p1 = session_manager.register(
            external_id="s1-p1",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=project1.id,
        )
        s2_p1 = session_manager.register(
            external_id="s2-p1",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=project1.id,
        )

        # Create sessions in project2
        s1_p2 = session_manager.register(
            external_id="s1-p2",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=project2.id,
        )

        # Project1 sessions should have seq_num 1 and 2
        assert s1_p1.seq_num == 1
        assert s2_p1.seq_num == 2

        # Project2 session should have seq_num 1 (independent from project1)
        assert s1_p2.seq_num == 1

    def test_resolve_session_reference_with_project_id(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        session_manager: SessionManager,
        temp_db: HubDatabase,
    ) -> None:
        """Test resolving #N format with project_id parameter."""
        from gobby.storage.projects import LocalProjectManager

        proj_manager = LocalProjectManager(temp_db)
        project1 = isolated_checkout_factory(proj_manager.db, "proj1").project
        project2 = isolated_checkout_factory(proj_manager.db, "proj2").project

        # Create #1 in each project
        s1 = session_manager.register(
            external_id="s1", machine_id=LOCAL_MACHINE_ID, source="claude", project_id=project1.id
        )
        s2 = session_manager.register(
            external_id="s2", machine_id=LOCAL_MACHINE_ID, source="claude", project_id=project2.id
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
        sample_project: dict[str, Any],
    ) -> None:
        """Test that resolve_session_reference raises ValueError for #N without project_id."""
        session = session_manager.register(
            external_id="global-test",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=sample_project["id"],
        )

        # Without project_id, #N resolution must fail (seq_num is per-project)
        with pytest.raises(ValueError, match="project context is required"):
            session_manager.resolve_session_reference(f"#{session.seq_num}")

    def test_resolve_session_reference_uuid_format(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test that UUID format still works with project-scoped resolution."""
        session = session_manager.register(
            external_id="uuid-test",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=sample_project["id"],
        )

        # UUID should resolve regardless of project_id
        resolved = session_manager.resolve_session_reference(session.id)
        assert resolved == session.id

    def test_resolve_session_reference_not_found(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test ValueError raised when session not found."""
        with pytest.raises(ValueError, match="not found"):
            session_manager.resolve_session_reference("#999", project_id=sample_project["id"])


class TestProjectQualifiedSeqNum:
    """'<project>#N' resolves a seq_num inside a named project."""

    @pytest.fixture
    def two_projects(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        session_manager: SessionManager,
        temp_db: HubDatabase,
    ) -> dict[str, Any]:
        from gobby.storage.projects import LocalProjectManager

        proj_manager = LocalProjectManager(temp_db)
        gobby = isolated_checkout_factory(proj_manager.db, "gobby-main").project
        goblins = isolated_checkout_factory(proj_manager.db, "game-goblins").project
        gobby_s1 = session_manager.register(
            external_id="gobby-1",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=gobby.id,
        )
        goblins_s1 = session_manager.register(
            external_id="goblins-1",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=goblins.id,
        )
        return {
            "gobby": gobby,
            "goblins": goblins,
            "gobby_s1": gobby_s1,
            "goblins_s1": goblins_s1,
        }

    def test_resolves_by_project_name_ignoring_caller_project(
        self, session_manager: SessionManager, two_projects: dict[str, Any]
    ) -> None:
        resolved = session_manager.resolve_session_reference(
            "game-goblins#1", project_id=two_projects["gobby"].id
        )
        assert resolved == two_projects["goblins_s1"].id

    def test_resolves_by_project_uuid(
        self, session_manager: SessionManager, two_projects: dict[str, Any]
    ) -> None:
        ref = f"{two_projects['gobby'].id}#1"
        assert session_manager.resolve_session_reference(ref) == two_projects["gobby_s1"].id

    def test_unknown_project_raises(
        self, session_manager: SessionManager, two_projects: dict[str, Any]
    ) -> None:
        with pytest.raises(ValueError, match="project 'nope' not found"):
            session_manager.resolve_session_reference("nope#1")

    def test_unknown_seq_num_raises_with_project(
        self, session_manager: SessionManager, two_projects: dict[str, Any]
    ) -> None:
        with pytest.raises(ValueError, match="Session #99 not found in project 'game-goblins'"):
            session_manager.resolve_session_reference("game-goblins#99")

    def test_bare_seq_num_miss_hints_qualified_form(
        self, session_manager: SessionManager, two_projects: dict[str, Any]
    ) -> None:
        with pytest.raises(ValueError, match=r"<project>#N"):
            session_manager.resolve_session_reference("#99", project_id=two_projects["gobby"].id)


class TestResolveReferenceExternalId:
    """external_id fallback resolution (Change 1)."""

    def _seed_session_with_external_uuid(
        self,
        session_manager: SessionManager,
        project_id: str,
        *,
        external_id: str,
        source: str = "claude",
        machine_id: str = LOCAL_MACHINE_ID,
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
        sample_project: dict[str, Any],
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
        sample_project: dict[str, Any],
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
        sample_project: dict[str, Any],
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

    @pytest.mark.parametrize(
        ("literal", "wildcard_match"),
        [("_", "X"), ("%", "X"), ("\\", "")],
    )
    def test_resolve_reference_treats_like_wildcards_as_literals(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
        literal: str,
        wildcard_match: str,
    ) -> None:
        prefix = f"literal{literal}prefix"
        expected = session_manager.register(
            external_id=f"{prefix}-target",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id=f"literal{wildcard_match}prefix-decoy",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )

        resolved = session_manager.resolve_session_reference(
            prefix, project_id=sample_project["id"]
        )

        assert resolved == expected.id

    def test_resolve_reference_prefers_id_match_over_external_id_match(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
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
            machine_id=LOCAL_MACHINE_ID,
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
        sample_project: dict[str, Any],
    ) -> None:
        """Two rows with the same external_id in a project → ValueError."""
        import uuid as _uuid

        external_uuid = str(_uuid.uuid4())
        self._seed_session_with_external_uuid(
            session_manager,
            sample_project["id"],
            external_id=external_uuid,
            source="claude",
            machine_id=LOCAL_MACHINE_ID,
        )
        self._seed_session_with_external_uuid(
            session_manager,
            sample_project["id"],
            external_id=external_uuid,
            source="codex",
            machine_id=LOCAL_MACHINE_ID,
        )
        with pytest.raises(ValueError, match="Ambiguous"):
            session_manager.resolve_session_reference(
                external_uuid, project_id=sample_project["id"]
            )

    def test_resolve_reference_external_id_cross_project_no_scope_ambiguous_raises(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        session_manager: SessionManager,
        temp_db: HubDatabase,
    ) -> None:
        """Same external_id across two projects with project_id=None → ValueError.

        Inserts directly via SQL because ``SessionManager.register()`` has
        a cross-project recovery path that overwrites the first row's
        ``project_id`` instead of creating a second row.
        """
        import uuid as _uuid
        from datetime import UTC, datetime

        from gobby.storage.machines import LocalMachineManager
        from gobby.storage.projects import LocalProjectManager

        LocalMachineManager(temp_db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID)
        pm = LocalProjectManager(temp_db)
        p1 = isolated_checkout_factory(pm.db, "amb-p1").project
        p2 = isolated_checkout_factory(pm.db, "amb-p2").project
        external_uuid = str(_uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        for idx, pid in enumerate((p1.id, p2.id)):
            temp_db.execute(
                """
                INSERT INTO sessions (
                    id, external_id, machine_id, source, project_id,
                    status, created_at, updated_at, seq_num
                ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s)
                """,
                (
                    str(_uuid.uuid4()),
                    external_uuid,
                    LOCAL_MACHINE_ID,
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
        sample_project: dict[str, Any],
    ) -> None:
        """Multiple external_ids sharing a prefix → ValueError."""
        shared_prefix = "deadbeef-0000"
        session_manager.register(
            external_id=f"{shared_prefix}-aaaa-aaaaaaaaaaaa",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id=f"{shared_prefix}-bbbb-bbbbbbbbbbbb",
            machine_id=LOCAL_MACHINE_ID,
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
        sample_project: dict[str, Any],
    ) -> None:
        """Unknown UUID → ValueError (not found)."""
        import uuid as _uuid

        with pytest.raises(ValueError, match="not found"):
            session_manager.resolve_session_reference(
                str(_uuid.uuid4()), project_id=sample_project["id"]
            )


@pytest.mark.parametrize("project_name", ["literal-S", "hyphenated-project", "part#name"])
def test_qualified_ref_uses_final_hash_and_literal_project_name(
    session_manager: SessionManager,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    project_name: str,
) -> None:
    project = isolated_checkout_factory(temp_db, "qualified-project").project
    with temp_db.transaction() as conn:
        conn.execute("UPDATE projects SET name = %s WHERE id = %s", (project_name, project.id))
    session = session_manager.register(
        external_id="qualified-session",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=project.id,
    )
    assert session.ref == f"{project_name}#{session.seq_num}"
    assert session_manager.resolve_session_reference(session.ref) == session.id
    assert session_manager.resolve_session_reference(str(session.seq_num), project.id) == session.id
    assert session_manager.resolve_session_reference(session.id) == session.id
    assert {item.ref for item in session_manager.list(project_id=project.id)} == {session.ref}


@pytest.mark.parametrize("ref", ["##1", "project#", "project#-1", "project#1x", "project#1\n"])
def test_malformed_qualified_ref_is_rejected(session_manager: SessionManager, ref: str) -> None:
    from gobby.storage.session_resolution import is_project_qualified_session_ref

    assert not is_project_qualified_session_ref(ref)
    with pytest.raises(ValueError):
        session_manager.resolve_session_reference(ref)


def test_deleted_project_and_retired_alias_are_not_resolved(
    session_manager: SessionManager,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
) -> None:
    project = isolated_checkout_factory(temp_db, "qualified-deleted").project
    session = session_manager.register(
        external_id="deleted-project-session",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=project.id,
    )
    with pytest.raises(ValueError, match="project.*not found"):
        session_manager.resolve_session_reference(f"{project.name}-S#{session.seq_num}")
    with temp_db.transaction() as conn:
        conn.execute("UPDATE projects SET deleted_at = NOW() WHERE id = %s", (project.id,))
    for qualifier in (project.name, project.id):
        with pytest.raises(ValueError, match="project.*not found"):
            session_manager.resolve_session_reference(f"{qualifier}#{session.seq_num}")
    assert session_manager.resolve_session_reference(session.id) == session.id


def test_canonical_ref_fallbacks_and_serialization(
    session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    session = session_manager.register(
        external_id="ref-fallbacks",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
    )
    assert session.ref == f"test-project#{session.seq_num}"
    assert session.to_dict()["ref"] == session.to_brief()["ref"] == session.ref
    for name in (None, "", "  "):
        assert replace(session, project_name=name).ref == f"{session.project_id}#{session.seq_num}"
    assert replace(session, seq_num=None).ref == session.id


@pytest.mark.parametrize(
    "prefix", ["(test-project-S#{seq})", "(test-project#{seq})", "old-project#{seq}"]
)
def test_automatic_title_normalization_preserves_suffix_source_and_manual_title(
    session_manager: SessionManager, sample_project: dict[str, Any], prefix: str
) -> None:
    automatic = session_manager.register(
        external_id="automatic-title",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
    )
    manual = session_manager.register(
        external_id="manual-title",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
        title="(old-S#1): My manual title",
    )
    old = prefix.format(seq=automatic.seq_num) + ": Task #42 - Keep: exact suffix"
    session_manager.update_title(automatic.id, old, title_source="task")
    changed: list[tuple[str, str]] = []
    session_manager.register_title_listener(lambda sid, title: changed.append((sid, title)))
    expected = f"{automatic.ref}: Task #42 - Keep: exact suffix"
    with patch("gobby.sessions.tmux_window_naming.schedule_tmux_window_rename") as rename:
        assert session_manager.normalize_automatic_title_refs() == 1
        assert session_manager.normalize_automatic_title_refs() == 0
        rename.assert_called_once()
        assert rename.call_args.args[1] == expected
    saved = session_manager.get(automatic.id)
    assert saved is not None
    assert (saved.title, saved.title_source) == (expected, "task")
    assert changed == [(automatic.id, expected)]
    saved_manual = session_manager.get(manual.id)
    assert saved_manual is not None
    assert (saved_manual.title, saved_manual.title_source) == (manual.title, "manual")
