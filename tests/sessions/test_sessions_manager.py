"""Tests for the SessionManager service layer."""

from importlib import import_module
from unittest.mock import MagicMock

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


@pytest.fixture
def session_storage(temp_db: LocalDatabase) -> SessionManager:
    """Create session storage with temp database."""
    return SessionManager(temp_db)


@pytest.fixture
def project_storage(temp_db: LocalDatabase) -> LocalProjectManager:
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
            machine_id="machine-abc",
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
            machine_id="machine-xyz",
            source="gemini",
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
            machine_id="machine",
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
            machine_id="machine",
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
            machine_id="machine",
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
            machine_id="machine-1",
            source="codex",
            project_id=test_project["id"],
        )

        # Clear cache
        session_mgr._session_mapping.clear()

        # Should look up from database (requires full composite key)
        result = session_mgr.lookup_session_id(
            external_id="db-lookup",
            source="codex",
            machine_id="machine-1",
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
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        # Clear cache
        session_mgr._session_mapping.clear()

        # Lookup with full composite key
        session_mgr.lookup_session_id(
            external_id="cache-lookup",
            source="claude",
            machine_id="machine",
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
            machine_id="machine",
            source="gemini",
            project_id=test_project["id"],
            title="Full Data Session",
        )

        session = session_mgr.get(session_id)
        assert session is not None
        assert session.id == session_id
        assert session.external_id == "full-data"
        assert session.source == "gemini"
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
            machine_id="machine-1",
            source="codex",
            project_id=test_project["id"],
            title="Canonical Session",
            transcript_path="/tmp/rollout.jsonl",
            terminal_context={"tmux_pane": "%1"},
        )
        session_storage.register(
            external_id="shared-external-id",
            machine_id="machine-1",
            source="claude",
            project_id=test_project["id"],
            title=None,
            transcript_path=None,
        )

        recovered = session_mgr.recover_session(
            external_id="shared-external-id",
            source="gemini",
            machine_id="machine-1",
            project_id=test_project["id"],
        )

        assert recovered is not None
        assert recovered.id == preferred.id
        assert session_mgr.get_session_id("shared-external-id", "gemini") == preferred.id

    def test_recover_session_returns_none_for_ambiguous_candidates(
        self,
        session_mgr: SessionManager,
        session_storage: SessionManager,
        test_project: dict,
    ) -> None:
        """Recovery should refuse ties instead of silently picking a row."""
        session_storage.register(
            external_id="ambiguous-external-id",
            machine_id="machine-1",
            source="codex",
            project_id=test_project["id"],
        )
        session_storage.register(
            external_id="ambiguous-external-id",
            machine_id="machine-1",
            source="claude",
            project_id=test_project["id"],
        )

        recovered = session_mgr.recover_session(
            external_id="ambiguous-external-id",
            source="gemini",
            machine_id="machine-1",
            project_id=test_project["id"],
        )

        assert recovered is None
        assert session_mgr.get_session_id("ambiguous-external-id", "gemini") is None


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
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        result = session_mgr.update_session_status(session_id, "paused")
        assert result is True

        session = session_mgr.get(session_id)
        assert session is not None
        assert session.status == "paused"

    def test_mark_session_expired(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test marking session as expired."""
        session_id = session_mgr.register_session(
            external_id="expire-test",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        result = session_mgr.mark_session_expired(session_id)
        assert result is True

        session = session_mgr.get(session_id)
        assert session is not None
        assert session.status == "expired"

    def test_update_nonexistent_session(self, session_mgr: SessionManager) -> None:
        """Test updating status of nonexistent session."""
        result = session_mgr.update_session_status("nonexistent", "active")
        assert result is False


class TestSessionManagerHandoff:
    """Tests for session handoff functionality."""

    def test_find_parent_session(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test finding parent session for handoff."""
        # Create and mark a session as handoff_ready
        parent_id = session_mgr.register_session(
            external_id="parent-cli",
            machine_id="handoff-machine",
            source="claude",
            project_id=test_project["id"],
        )
        session_mgr.update_session_status(parent_id, "handoff_ready")

        # Update summary for handoff
        session_mgr._storage.update_summary(parent_id, summary_markdown="Test summary content")

        # Find parent (with very short max_attempts for test speed)
        result = session_mgr.find_parent_session(
            machine_id="handoff-machine",
            source="claude",
            project_id=test_project["id"],
            max_attempts=1,
        )

        assert result is not None
        found_id, summary = result
        assert found_id == parent_id
        assert summary == "Test summary content"

    def test_find_parent_session_no_handoff_ready(
        self,
        session_mgr: SessionManager,
        test_project: dict,
    ) -> None:
        """Test finding parent when none marked handoff_ready."""
        # Create an active session
        session_mgr.register_session(
            external_id="active-session",
            machine_id="test-machine",
            source="claude",
            project_id=test_project["id"],
        )

        # Should not find any parent (max_attempts=1 for speed)
        result = session_mgr.find_parent_session(
            machine_id="test-machine",
            source="claude",
            project_id=test_project["id"],
            max_attempts=1,
        )

        assert result is None


class TestSessionManagerCaching:
    """Tests for session caching functionality."""

    def test_cache_session_mapping(self, session_mgr: SessionManager) -> None:
        """Test manually caching session mapping."""
        session_mgr.cache_session_mapping("manual-cli", "claude", "manual-session-id")

        result = session_mgr.get_session_id("manual-cli", "claude")
        assert result == "manual-session-id"

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
                    machine_id=f"machine-{index}",
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
