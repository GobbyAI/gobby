"""Tests for transcript backup and restore utilities."""

import gzip
from pathlib import Path

import pytest

from gobby.sessions.transcript_archive import (
    backup_transcript,
    get_archive_dir,
    restore_transcript,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def archive_dir(tmp_path: Path) -> str:
    """Provide a temporary archive directory."""
    d = tmp_path / "archives"
    d.mkdir()
    return str(d)


@pytest.fixture()
def sample_jsonl(tmp_path: Path) -> Path:
    """Create a sample JSONL file."""
    f = tmp_path / "test-session.jsonl"
    f.write_text('{"type":"message","role":"user","content":"hello"}\n')
    return f


class TestGetArchiveDir:
    def test_default_creates_dir(self, tmp_path: Path) -> None:
        custom = str(tmp_path / "custom_archive")
        result = get_archive_dir(custom)
        assert result == Path(custom)
        assert result.is_dir()

    def test_override_path(self, tmp_path: Path) -> None:
        custom = str(tmp_path / "my_archives")
        result = get_archive_dir(custom)
        assert result == Path(custom)
        assert result.is_dir()


class TestBackupTranscript:
    def test_backup_creates_valid_gzip(
        self, sample_jsonl: Path, archive_dir: str
    ) -> None:
        result = backup_transcript("ext-123", str(sample_jsonl), archive_dir)
        assert result is not None

        archive_path = Path(result)
        assert archive_path.exists()
        assert archive_path.name == "ext-123.jsonl.gz"

        # Verify it's valid gzip with correct content
        with gzip.open(archive_path, "rt") as f:
            content = f.read()
        assert "hello" in content

    def test_backup_missing_source_returns_none(self, archive_dir: str) -> None:
        result = backup_transcript("ext-456", "/nonexistent/file.jsonl", archive_dir)
        assert result is None

    def test_backup_overwrites_existing(
        self, sample_jsonl: Path, archive_dir: str
    ) -> None:
        # First backup
        backup_transcript("ext-789", str(sample_jsonl), archive_dir)

        # Modify source and backup again
        sample_jsonl.write_text('{"type":"message","role":"user","content":"updated"}\n')
        result = backup_transcript("ext-789", str(sample_jsonl), archive_dir)
        assert result is not None

        with gzip.open(result, "rt") as f:
            content = f.read()
        assert "updated" in content


class TestRestoreTranscript:
    def test_restore_decompresses_correctly(
        self, sample_jsonl: Path, archive_dir: str, tmp_path: Path
    ) -> None:
        original_content = sample_jsonl.read_text()

        # Backup first
        backup_transcript("ext-restore", str(sample_jsonl), archive_dir)

        # Delete original
        restore_target = tmp_path / "restored.jsonl"
        assert not restore_target.exists()

        # Restore
        result = restore_transcript("ext-restore", str(restore_target), archive_dir)
        assert result is True
        assert restore_target.read_text() == original_content

    def test_restore_noop_when_original_exists(
        self, sample_jsonl: Path, archive_dir: str
    ) -> None:
        backup_transcript("ext-noop", str(sample_jsonl), archive_dir)

        # Restore to same path — should be a no-op
        result = restore_transcript("ext-noop", str(sample_jsonl), archive_dir)
        assert result is False

    def test_restore_missing_archive_returns_false(
        self, archive_dir: str, tmp_path: Path
    ) -> None:
        target = tmp_path / "missing.jsonl"
        result = restore_transcript("no-such-session", str(target), archive_dir)
        assert result is False

    def test_restore_corrupt_archive(
        self, archive_dir: str, tmp_path: Path
    ) -> None:
        # Write corrupt data as a .gz file
        corrupt = Path(archive_dir) / "corrupt-session.jsonl.gz"
        corrupt.write_bytes(b"this is not gzip data")

        target = tmp_path / "from_corrupt.jsonl"
        result = restore_transcript("corrupt-session", str(target), archive_dir)
        assert result is False
        # Partial file should be cleaned up
        assert not target.exists()

    def test_restore_creates_parent_directories(
        self, sample_jsonl: Path, archive_dir: str, tmp_path: Path
    ) -> None:
        backup_transcript("ext-dirs", str(sample_jsonl), archive_dir)

        nested_target = tmp_path / "deep" / "nested" / "dir" / "transcript.jsonl"
        result = restore_transcript("ext-dirs", str(nested_target), archive_dir)
        assert result is True
        assert nested_target.exists()

    def test_custom_archive_dir(
        self, sample_jsonl: Path, tmp_path: Path
    ) -> None:
        custom_dir = str(tmp_path / "custom")
        backup_transcript("ext-custom", str(sample_jsonl), custom_dir)

        target = tmp_path / "restored_custom.jsonl"
        result = restore_transcript("ext-custom", str(target), custom_dir)
        assert result is True
        assert target.read_text() == sample_jsonl.read_text()
