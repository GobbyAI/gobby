"""Tests for the SessionManager service layer."""

import itertools
import uuid
from collections.abc import Iterator
from importlib import import_module
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID, LocalProjectManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def session_storage(temp_db: HubDatabase) -> SessionManager:
    """Create session storage with temp database."""
    return SessionManager(temp_db)


@pytest.fixture
def project_storage(temp_db: HubDatabase) -> LocalProjectManager:
    """Create project storage with temp database."""
    return LocalProjectManager(temp_db)


@pytest.fixture
def test_project(project_storage: LocalProjectManager) -> dict:
    """Create a test project."""
    project = project_storage.create(name="test-project", repo_path="/tmp/test")
    return project.to_dict()


@pytest.fixture
def session_mgr(session_storage: SessionManager) -> SessionManager:
    """Create a SessionManager instance for testing."""
    return SessionManager(
        session_storage=session_storage,
        logger_instance=MagicMock(),
    )


class TestSessionManagerRegistration:
    """Tests for session registration."""

    def test_register_session(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test registering a new session."""
        session_id = session_mgr.register_session(
            external_id="test-cli-123",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )

        assert session_id is not None
        assert len(session_id) == 36  # UUID format

    def test_register_session_with_all_fields(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test registering session with all optional fields."""
        session_id = session_mgr.register_session(
            external_id="full-cli-123",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="qwen",
            project_id=test_project["id"],
            parent_session_id=None,  # Use None instead of invalid UUID
            transcript_path="/path/to/transcript.jsonl",
            title="Test Session Title",
            git_branch="feature/test",
        )

        assert session_id is not None
        # Verify session data
        session = session_mgr.get(session_id)
        assert session is not None
        assert session.title == "Test Session Title"
        assert session.git_branch == "feature/test"

    def test_register_caches_mapping(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test that registration caches external_id -> session_id mapping."""
        session_id = session_mgr.register_session(
            external_id="cached-cli",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )

        # Should be cached (keyed by (external_id, source))
        cached_id = session_mgr.get_session_id("cached-cli", "claude")
        assert cached_id == session_id

    def test_register_extracts_git_branch(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test git branch is set when provided."""
        session_id = session_mgr.register_session(
            external_id="git-test",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
            git_branch="main",
        )

        session = session_mgr.get(session_id)
        assert session is not None
        assert session.git_branch == "main"


class TestSessionManagerLookup:
    """Tests for session lookup."""

    def test_get_session_id_cached(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test getting cached session_id."""
        session_id = session_mgr.register_session(
            external_id="lookup-test",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )

        result = session_mgr.get_session_id("lookup-test", "claude")
        assert result == session_id

    def test_get_session_id_not_cached(self, session_mgr: SessionManager) -> None:
        """Test getting session_id when not cached returns None."""
        result = session_mgr.get_session_id("nonexistent", "claude")
        assert result is None

    def test_lookup_session_id(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test looking up session_id from database."""
        session_id = session_mgr.register_session(
            external_id="db-lookup",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=test_project["id"],
        )

        # Clear cache
        session_mgr._session_mapping.clear()

        # Should look up from database (requires full composite key)
        result = session_mgr.lookup_session_id(
            external_id="db-lookup",
            source="codex",
            project_id=test_project["id"],
        )
        assert result == session_id

    def test_lookup_session_id_caches_result(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test that lookup caches the result."""
        session_id = session_mgr.register_session(
            external_id="cache-lookup",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )

        # Clear cache
        session_mgr._session_mapping.clear()

        # Lookup with full composite key
        session_mgr.lookup_session_id(
            external_id="cache-lookup",
            source="claude",
            project_id=test_project["id"],
        )

        # Should now be cached (keyed by (external_id, source))
        assert session_mgr.get_session_id("cache-lookup", "claude") == session_id

    def test_get_session(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test getting full session data."""
        session_id = session_mgr.register_session(
            external_id="full-data",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="qwen",
            project_id=test_project["id"],
            title="Full Data Session",
        )

        session = session_mgr.get(session_id)
        assert session is not None
        assert session.id == session_id
        assert session.external_id == "full-data"
        assert session.source == "qwen"
        assert session.title == "Full Data Session"

    def test_get_session_nonexistent(self, session_mgr: SessionManager) -> None:
        """Test getting nonexistent session returns None."""
        result = session_mgr.get("nonexistent-uuid")
        assert result is None

    def test_recover_session_prefers_metadata_rich_candidate(
        self,
        session_mgr: SessionManager,
        session_storage: SessionManager,
        test_project: dict,
    ) -> None:
        """Cross-source recovery should prefer the candidate with transcript metadata."""
        preferred = session_storage.register(
            external_id="shared-external-id",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=test_project["id"],
            title="Canonical Session",
            transcript_path="/tmp/rollout.jsonl",
            terminal_context={"tmux_pane": "%1"},
        )
        session_storage.register(
            external_id="shared-external-id",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
            title=None,
            transcript_path=None,
        )

        recovered = session_mgr.recover_session(
            external_id="shared-external-id",
            source="qwen",
            project_id=test_project["id"],
        )

        assert recovered is not None
        assert recovered.id == preferred.id
        # Cross-source recovery caches the mapping under the requesting source;
        # the validated getter rejects cross-source reads, so assert the raw cache.
        assert (
            session_mgr._session_mapping[
                ("shared-external-id", "qwen", test_project["id"], "terminal")
            ]
            == preferred.id
        )

    def test_recover_session_rejects_score_ties_even_when_rank_differs(
        self,
        session_mgr: SessionManager,
        session_storage: SessionManager,
        test_project: dict,
    ) -> None:
        """Recovery should reject same-quality rows even when rank differs."""
        session_storage.register(
            external_id="ranked-external-id",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=test_project["id"],
        )
        session_storage.register(
            external_id="ranked-external-id",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )

        recovered = session_mgr.recover_session(
            external_id="ranked-external-id",
            source="qwen",
            project_id=test_project["id"],
        )

        assert recovered is None
        assert session_mgr.get_session_id("ranked-external-id", "qwen") is None

    def test_recover_session_none_project_searches_real_project(
        self,
        session_mgr: SessionManager,
        session_storage: SessionManager,
        test_project: dict,
    ) -> None:
        real_project = session_storage.register(
            external_id="real-project-external-id",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=test_project["id"],
        )

        recovered = session_mgr.recover_session(
            external_id="real-project-external-id",
            source="qwen",
            project_id=None,
        )

        assert recovered is not None
        assert recovered.id == real_project.id
        assert recovered.project_id != PERSONAL_PROJECT_ID

    def test_recover_session_none_project_cross_project_collision_returns_none(
        self,
        session_mgr: SessionManager,
        session_storage: SessionManager,
        project_storage: LocalProjectManager,
        test_project: dict,
    ) -> None:
        other_project = project_storage.create(name="other-project", repo_path="/tmp/other")
        session_storage.register(
            external_id="colliding-external-id",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=test_project["id"],
        )
        session_storage.register(
            external_id="colliding-external-id",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=other_project.id,
        )

        recovered = session_mgr.recover_session(
            external_id="colliding-external-id",
            source="qwen",
            project_id=None,
        )

        assert recovered is None


class TestSessionManagerStatus:
    """Tests for session status management."""

    def test_update_session_status(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test updating session status."""
        session_id = session_mgr.register_session(
            external_id="status-test",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )

        result = session_mgr.update_session_status(session_id, "paused")
        assert result is True

        session = session_mgr.get(session_id)
        assert session is not None
        assert session.status == "paused"

    def test_update_session_status_routes_confirmed_activity(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        session_id = session_mgr.register_session(
            external_id="activity-status-test",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )
        session_mgr.update_status(session_id, "expired")
        session_mgr.mark_transcript_processed(session_id)

        result = session_mgr.update_session_status(
            session_id,
            "active",
            activity_confirmed=True,
        )

        assert result is True
        updated = session_mgr.get(session_id)
        assert updated is not None
        assert updated.status == "active"
        row = session_mgr.db.fetchone(
            "SELECT transcript_processed FROM sessions WHERE id = %s",
            (session_id,),
        )
        assert row is not None
        assert row["transcript_processed"] is False

    def test_mark_session_expired(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test marking session as expired."""
        session_id = session_mgr.register_session(
            external_id="expire-test",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )

        result = session_mgr.mark_session_expired(session_id, cause="context_reuse")
        assert result is True

        session = session_mgr.get(session_id)
        assert session is not None
        assert session.status == "expired"

    def test_update_nonexistent_session(self, session_mgr: SessionManager) -> None:
        """Test updating status of nonexistent session."""
        result = session_mgr.update_session_status("nonexistent", "active")
        assert result is False

        confirmed_result = session_mgr.update_session_status(
            "00000000-0000-0000-0000-000000000000",
            "active",
            activity_confirmed=True,
        )
        assert confirmed_result is False


class TestSessionManagerCaching:
    """Tests for session caching functionality."""

    def test_cache_session_mapping(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test manually caching session mapping."""
        session_id = session_mgr.register_session(
            external_id="manual-cli",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=test_project["id"],
        )
        session_mgr._session_mapping.clear()

        session_mgr.cache_session_mapping(
            "manual-cli", "claude", session_id, project_id=test_project["id"]
        )

        result = session_mgr.get_session_id("manual-cli", "claude")
        assert result == session_id

    def test_cache_session_mapping_is_scoped_by_project(
        self,
        temp_db: HubDatabase,
        session_mgr: SessionManager,
        project_storage: LocalProjectManager,
    ) -> None:
        project_a = project_storage.create(name="project-a", repo_path="/tmp/project-a")
        project_b = project_storage.create(name="project-b", repo_path="/tmp/project-b")
        # register() unifies machine-neutral identity across projects, so seed
        # two same-source rows in different projects directly.
        session_a = str(uuid.uuid4())
        session_b = str(uuid.uuid4())
        with temp_db.transaction() as conn:
            for session_id, project_id in ((session_a, project_a.id), (session_b, project_b.id)):
                conn.execute(
                    "INSERT INTO sessions (id, external_id, machine_id, source, project_id) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (session_id, "shared-cli", LOCAL_MACHINE_ID, "claude", project_id),
                )
        session_mgr._session_mapping.clear()

        session_mgr.cache_session_mapping(
            "shared-cli",
            "claude",
            session_a,
            project_id=project_a.id,
        )
        session_mgr.cache_session_mapping(
            "shared-cli",
            "claude",
            session_b,
            project_id=project_b.id,
        )

        assert (
            session_mgr.get_session_id("shared-cli", "claude", project_id=project_a.id) == session_a
        )
        assert (
            session_mgr.get_session_id("shared-cli", "claude", project_id=project_b.id) == session_b
        )
        assert session_mgr.get_session_id("shared-cli", "claude") is None

    def test_cache_session_mapping_expires_entries(self, session_mgr: SessionManager) -> None:
        with patch(
            "gobby.storage.sessions._registration_cache.time.monotonic",
            side_effect=[0.0, 3600.0],
        ):
            session_mgr.cache_session_mapping(
                "expiring-cli",
                "claude",
                "session-id",
                project_id="project",
            )
            assert (
                session_mgr.get_session_id("expiring-cli", "claude", project_id="project") is None
            )

    def test_cache_session_mapping_evicts_oldest_entry(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        session_ids = {
            suffix: session_mgr.register_session(
                external_id=f"cli-{suffix}",
                machine_id=LOCAL_MACHINE_ID,
                source="claude",
                project_id=test_project["id"],
            )
            for suffix in ("a", "b", "c")
        }
        session_mgr._session_mapping.clear()
        with (
            patch("gobby.storage.sessions._registration_cache._SESSION_MAPPING_MAX_ENTRIES", 2),
            patch(
                "gobby.storage.sessions._registration_cache.time.monotonic",
                side_effect=itertools.count(0.0, 1.0),
            ),
        ):
            for suffix in ("a", "b", "c"):
                session_mgr.cache_session_mapping(
                    f"cli-{suffix}",
                    "claude",
                    session_ids[suffix],
                    project_id=test_project["id"],
                )

            assert (
                session_mgr.get_session_id("cli-a", "claude", project_id=test_project["id"]) is None
            )
            assert (
                session_mgr.get_session_id("cli-c", "claude", project_id=test_project["id"])
                == session_ids["c"]
            )

    def test_thread_safety(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test that session operations are thread-safe."""
        import threading

        results = []
        errors = []

        def register_session(index: int):
            try:
                session_id = session_mgr.register_session(
                    external_id=f"thread-{index}",
                    machine_id=LOCAL_MACHINE_ID,
                    source="claude",
                    project_id=test_project["id"],
                )
                results.append((index, session_id))
            except Exception as e:
                errors.append((index, e))

        threads = [threading.Thread(target=register_session, args=(i,)) for i in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All registrations should succeed
        assert len(errors) == 0
        assert len(results) == 5

        # All session IDs should be unique
        session_ids = [sid for _, sid in results]
        assert len(set(session_ids)) == 5


def test_session_manager_import_is_storage_canonical() -> None:
    """Canonical SessionManager should come from gobby.storage.sessions."""
    storage_sessions = import_module("gobby.storage.sessions")
    canonical_cls = getattr(storage_sessions, "SessionManager", None)

    assert canonical_cls is not None
    assert SessionManager is canonical_cls
