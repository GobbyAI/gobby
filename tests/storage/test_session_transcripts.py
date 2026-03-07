"""Tests for LocalSessionTranscriptManager."""

import hashlib
import os

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_transcripts import (
    LocalSessionTranscriptManager,
    TranscriptSnapshotThrottle,
)
from gobby.storage.sessions import LocalSessionManager

pytestmark = pytest.mark.unit


@pytest.fixture
def transcript_manager(temp_db: LocalDatabase) -> LocalSessionTranscriptManager:
    return LocalSessionTranscriptManager(temp_db)


@pytest.fixture
def session_id(temp_db: LocalDatabase, session_manager: LocalSessionManager) -> str:
    project_manager = LocalProjectManager(temp_db)
    project = project_manager.get_or_create("/tmp/test-project")
    session = session_manager.register(
        external_id="test-ext-id",
        machine_id="test-machine",
        source="test",
        project_id=project.id,
        jsonl_path="/tmp/test-transcript.jsonl",
    )
    return session.id


SAMPLE_JSONL = b'{"type":"human","message":{"role":"user","content":"hello"}}\n{"type":"assistant","message":{"role":"assistant","content":"hi"}}\n'


class TestStoreAndRetrieve:
    def test_store_and_get(
        self,
        transcript_manager: LocalSessionTranscriptManager,
        session_id: str,
    ) -> None:
        stats = transcript_manager.store_transcript(session_id, SAMPLE_JSONL)

        assert stats["uncompressed_size"] == len(SAMPLE_JSONL)
        assert stats["compressed_size"] > 0
        assert stats["compressed_size"] < stats["uncompressed_size"]
        assert stats["checksum"].startswith("sha256:")

        raw = transcript_manager.get_transcript(session_id)
        assert raw == SAMPLE_JSONL

    def test_store_upsert(
        self,
        transcript_manager: LocalSessionTranscriptManager,
        session_id: str,
    ) -> None:
        transcript_manager.store_transcript(session_id, b"first version\n")
        transcript_manager.store_transcript(session_id, SAMPLE_JSONL)

        raw = transcript_manager.get_transcript(session_id)
        assert raw == SAMPLE_JSONL

    def test_get_nonexistent(
        self,
        transcript_manager: LocalSessionTranscriptManager,
    ) -> None:
        assert transcript_manager.get_transcript("nonexistent-id") is None


class TestHasTranscript:
    def test_exists(
        self,
        transcript_manager: LocalSessionTranscriptManager,
        session_id: str,
    ) -> None:
        assert transcript_manager.has_transcript(session_id) is False
        transcript_manager.store_transcript(session_id, SAMPLE_JSONL)
        assert transcript_manager.has_transcript(session_id) is True


class TestDelete:
    def test_delete(
        self,
        transcript_manager: LocalSessionTranscriptManager,
        session_id: str,
    ) -> None:
        transcript_manager.store_transcript(session_id, SAMPLE_JSONL)
        assert transcript_manager.delete_transcript(session_id) is True
        assert transcript_manager.has_transcript(session_id) is False

    def test_delete_nonexistent(
        self,
        transcript_manager: LocalSessionTranscriptManager,
    ) -> None:
        assert transcript_manager.delete_transcript("nonexistent-id") is False


class TestGetStats:
    def test_stats(
        self,
        transcript_manager: LocalSessionTranscriptManager,
        session_id: str,
    ) -> None:
        transcript_manager.store_transcript(session_id, SAMPLE_JSONL)
        stats = transcript_manager.get_stats(session_id)

        assert stats is not None
        assert stats["exists"] is True
        assert stats["uncompressed_size"] == len(SAMPLE_JSONL)
        assert stats["compressed_size"] > 0
        expected_checksum = hashlib.sha256(SAMPLE_JSONL).hexdigest()
        assert stats["checksum"] == f"sha256:{expected_checksum}"
        assert "created_at" in stats
        assert "updated_at" in stats

    def test_stats_nonexistent(
        self,
        transcript_manager: LocalSessionTranscriptManager,
    ) -> None:
        assert transcript_manager.get_stats("nonexistent-id") is None


class TestRestoreToDisk:
    def test_restore(
        self,
        transcript_manager: LocalSessionTranscriptManager,
        session_id: str,
        tmp_path: pytest.TempPathFactory,
    ) -> None:
        transcript_manager.store_transcript(session_id, SAMPLE_JSONL)
        target = str(tmp_path / "restored.jsonl")  # type: ignore[operator]

        path = transcript_manager.restore_to_disk(session_id, target)
        assert path == target
        assert os.path.exists(target)
        with open(target, "rb") as f:
            assert f.read() == SAMPLE_JSONL

    def test_restore_to_original_path(
        self,
        transcript_manager: LocalSessionTranscriptManager,
        session_id: str,
        tmp_path: pytest.TempPathFactory,
    ) -> None:
        # Update session's jsonl_path to a temp location
        original_path = str(tmp_path / "original.jsonl")  # type: ignore[operator]
        transcript_manager.db.execute(
            "UPDATE sessions SET jsonl_path = ? WHERE id = ?",
            (original_path, session_id),
        )
        transcript_manager.store_transcript(session_id, SAMPLE_JSONL)

        path = transcript_manager.restore_to_disk(session_id)
        assert path == original_path
        with open(original_path, "rb") as f:
            assert f.read() == SAMPLE_JSONL

    def test_restore_no_blob(
        self,
        transcript_manager: LocalSessionTranscriptManager,
        session_id: str,
    ) -> None:
        assert transcript_manager.restore_to_disk(session_id) is None


class TestSnapshotThrottle:
    def test_first_snapshot_allowed(self) -> None:
        throttle = TranscriptSnapshotThrottle()
        assert throttle.should_snapshot("s1") is True

    def test_throttled_after_snapshot(self) -> None:
        throttle = TranscriptSnapshotThrottle(interval_seconds=60.0)
        throttle.record_snapshot("s1")
        assert throttle.should_snapshot("s1") is False

    def test_force_bypasses_throttle(self) -> None:
        throttle = TranscriptSnapshotThrottle(interval_seconds=60.0)
        throttle.record_snapshot("s1")
        assert throttle.should_snapshot("s1", force=True) is True

    def test_remove_resets_throttle(self) -> None:
        throttle = TranscriptSnapshotThrottle(interval_seconds=60.0)
        throttle.record_snapshot("s1")
        throttle.remove("s1")
        assert throttle.should_snapshot("s1") is True

    def test_different_sessions_independent(self) -> None:
        throttle = TranscriptSnapshotThrottle(interval_seconds=60.0)
        throttle.record_snapshot("s1")
        assert throttle.should_snapshot("s1") is False
        assert throttle.should_snapshot("s2") is True
