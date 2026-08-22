"""Tests for src/utils/machine_id.py - Machine ID Utility."""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.utils.durable_file import durable_replace_text
from gobby.utils.machine_id import (
    _generate_machine_id,
    _get_or_create_machine_id,
    clear_cache,
    get_machine_id,
    get_machine_id_file,
)

pytestmark = pytest.mark.unit


class TestGetMachineId:
    """Tests for get_machine_id function."""

    def setup_method(self):
        """Clear cache before each test."""
        clear_cache()

    def test_returns_cached_id_if_available(self) -> None:
        """Test that cached ID is returned without recalculating."""
        import gobby.utils.machine_id as machine_id_module

        # Set cached value directly
        machine_id_module._cached_machine_id = "21000000-0000-4000-8000-000000000018"

        result = get_machine_id()

        assert result == "21000000-0000-4000-8000-000000000018"

        # Cleanup
        machine_id_module._cached_machine_id = None

    def test_calls_get_or_create_when_no_cache(self) -> None:
        """Test that _get_or_create_machine_id is called when no cache."""
        with patch(
            "gobby.utils.machine_id._get_or_create_machine_id", return_value="new-machine-id"
        ) as mock:
            result = get_machine_id()

        assert result == "new-machine-id"
        mock.assert_called_once()

    def test_caches_result_after_call(self) -> None:
        """Test that result is cached after first call."""
        import gobby.utils.machine_id as machine_id_module

        with patch("gobby.utils.machine_id._get_or_create_machine_id", return_value="new-id"):
            get_machine_id()

        assert machine_id_module._cached_machine_id == "new-id"

        # Cleanup
        machine_id_module._cached_machine_id = None

    def test_propagates_os_error(self) -> None:
        """Test that OSError is propagated."""
        with patch(
            "gobby.utils.machine_id._get_or_create_machine_id", side_effect=OSError("File error")
        ):
            with pytest.raises(OSError, match="Failed to retrieve or create machine ID"):
                get_machine_id()


class TestGetOrCreateMachineId:
    """Tests for _get_or_create_machine_id function."""

    def test_returns_existing_id_from_file(self, tmp_path) -> None:
        """Test returns machine_id from file if present."""
        test_file = tmp_path / "machine_id"
        test_file.write_text("existing-id-from-file")
        test_file.chmod(0o644)

        with patch("gobby.utils.machine_id.get_machine_id_file", return_value=test_file):
            result = _get_or_create_machine_id()

        assert result == "existing-id-from-file"
        assert test_file.stat().st_mode & 0o777 == 0o600

    def test_generates_and_saves_new_id_when_file_missing(self, tmp_path) -> None:
        """Test generates new ID and saves to file when missing."""
        test_file = tmp_path / "machine_id"

        with (
            patch("gobby.utils.machine_id.get_machine_id_file", return_value=test_file),
            patch("gobby.utils.machine_id._generate_machine_id", return_value="new-generated-id"),
        ):
            result = _get_or_create_machine_id()

        assert result == "new-generated-id"
        assert test_file.exists()
        assert test_file.read_text() == "new-generated-id"

    def test_concurrent_creation_keeps_generated_id_stable(self, tmp_path) -> None:
        """Test concurrent creation leaves one stable machine ID."""
        test_file = tmp_path / "machine_id"
        first_generate = threading.Event()
        release_first = threading.Event()
        second_generate = threading.Event()
        generation_count = 0

        def generate() -> str:
            nonlocal generation_count
            generation_count += 1
            if generation_count == 1:
                first_generate.set()
                assert release_first.wait(timeout=1)
            else:
                second_generate.set()
            return f"generated-id-{generation_count}"

        with (
            patch("gobby.utils.machine_id.get_machine_id_file", return_value=test_file),
            patch(
                "gobby.utils.machine_id._generate_machine_id", side_effect=generate
            ) as mock_generate,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(_get_or_create_machine_id)
            assert first_generate.wait(timeout=1)
            second = executor.submit(_get_or_create_machine_id)
            try:
                assert not second_generate.wait(timeout=0.1)
            finally:
                release_first.set()

        assert first.result() == second.result() == "generated-id-1"
        assert test_file.read_text() == "generated-id-1"
        mock_generate.assert_called_once()

    def test_creates_parent_directory_if_missing(self, tmp_path) -> None:
        """Test creates parent directory if it doesn't exist."""
        test_file = tmp_path / "subdir" / "machine_id"

        with (
            patch("gobby.utils.machine_id.get_machine_id_file", return_value=test_file),
            patch("gobby.utils.machine_id._generate_machine_id", return_value="new-id"),
        ):
            result = _get_or_create_machine_id()

        assert result == "new-id"
        assert test_file.parent.exists()

    def test_ignores_empty_file(self, tmp_path) -> None:
        """Test generates new ID if file exists but is empty."""
        test_file = tmp_path / "machine_id"
        test_file.write_text("   \n")  # Whitespace only

        with (
            patch("gobby.utils.machine_id.get_machine_id_file", return_value=test_file),
            patch("gobby.utils.machine_id._generate_machine_id", return_value="new-id"),
        ):
            result = _get_or_create_machine_id()

        assert result == "new-id"

    def test_isolates_identity_by_gobby_home(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each explicit Gobby home owns its machine identity."""
        first_home = tmp_path / "first"
        second_home = tmp_path / "second"

        with patch(
            "gobby.utils.machine_id._generate_machine_id",
            side_effect=["first-id", "second-id"],
        ):
            monkeypatch.setenv("GOBBY_HOME", str(first_home))
            first = _get_or_create_machine_id()
            monkeypatch.setenv("GOBBY_HOME", str(second_home))
            second = _get_or_create_machine_id()

        assert first == "first-id"
        assert second == "second-id"
        assert (first_home / "machine_id").read_text() == "first-id"
        assert (second_home / "machine_id").read_text() == "second-id"


def test_machine_id_file_defaults_to_user_gobby_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOBBY_HOME", raising=False)
    with patch("gobby.paths.Path.home", return_value=tmp_path):
        assert get_machine_id_file() == tmp_path / ".gobby" / "machine_id"


class TestDurableReplace:
    """Tests for the shared durable replacement primitive."""

    def test_writes_content_to_file(self, tmp_path) -> None:
        """Test writes content correctly."""
        test_file = tmp_path / "test_file"

        durable_replace_text(test_file, "test-content")

        assert test_file.read_text() == "test-content"

    def test_sets_restrictive_permissions(self, tmp_path) -> None:
        """Test file is created with 0o600 permissions."""
        test_file = tmp_path / "test_file"

        durable_replace_text(test_file, "test-content")

        # Check permissions (owner read/write only)
        mode = test_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_overwrites_existing_file(self, tmp_path) -> None:
        """Test overwrites existing file content."""
        test_file = tmp_path / "test_file"
        test_file.write_text("old-content")
        test_file.chmod(0o644)

        durable_replace_text(test_file, "new-content")

        assert test_file.read_text() == "new-content"
        assert test_file.stat().st_mode & 0o777 == 0o600

    def test_failed_replace_preserves_existing_file(self, tmp_path) -> None:
        """Test replacement failure preserves existing content and removes temp file."""
        test_file = tmp_path / "test_file"
        test_file.write_text("old-content")

        with (
            patch("gobby.utils.durable_file.uuid.uuid4") as uuid4,
            patch("gobby.utils.durable_file.os.replace", side_effect=OSError("replace failed")),
            pytest.raises(OSError, match="replace failed"),
        ):
            uuid4.return_value.hex = "known"
            durable_replace_text(test_file, "new-content")

        assert test_file.read_text() == "old-content"
        assert not (tmp_path / ".test_file.known.tmp").exists()

    def test_fsyncs_file_and_parent_then_reads_back(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test_file"

        with (
            patch("gobby.utils.durable_file.os.fsync", wraps=os.fsync) as fsync,
            patch(
                "pathlib.Path.read_bytes",
                autospec=True,
                side_effect=Path.read_bytes,
            ) as read_bytes,
        ):
            durable_replace_text(test_file, "durable")

        assert fsync.call_count == 2
        assert read_bytes.call_count == 1


class TestGenerateMachineId:
    """Tests for _generate_machine_id function."""

    def test_generates_uuid4_unconditionally(self) -> None:
        expected = uuid.UUID("c37b7e38-6b2f-4c76-a53a-7da88f9d84cf")

        with patch("gobby.utils.machine_id.uuid.uuid4", return_value=expected) as uuid4:
            result = _generate_machine_id()

        assert result == str(expected)
        uuid4.assert_called_once_with()


class TestClearCache:
    """Tests for clear_cache function."""

    def test_clears_cached_value(self) -> None:
        """Test that clear_cache sets cached value to None."""
        import gobby.utils.machine_id as machine_id_module

        # Set a cached value
        machine_id_module._cached_machine_id = "21000000-0000-4000-8000-00000000001c"

        clear_cache()

        assert machine_id_module._cached_machine_id is None

    def test_clear_cache_is_thread_safe(self) -> None:
        """Test that clear_cache uses lock."""
        # The function uses _cache_lock internally
        # Just verify it doesn't raise any exceptions
        first = clear_cache()
        second = clear_cache()

        assert first is None
        assert second is None


class TestReadOnlyHome:
    """A present identity is readable where the home denies writes (#20712)."""

    def test_existing_id_is_read_without_lock_or_permission_repair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        machine_id_file = home / "machine_id"
        machine_id_file.write_text("sandboxed-identity\n")

        def deny_write(*args: object, **kwargs: object) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr("gobby.utils.machine_id.get_machine_id_file", lambda: machine_id_file)
        monkeypatch.setattr("gobby.utils.machine_id.exclusive_file_lock", deny_write)
        monkeypatch.setattr(Path, "chmod", deny_write)

        assert _get_or_create_machine_id() == "sandboxed-identity"
        assert sorted(p.name for p in home.iterdir()) == ["machine_id"]

    def test_missing_id_still_takes_the_creation_lock(self, tmp_path: Path) -> None:
        machine_id_file = tmp_path / "machine_id"

        with (
            patch("gobby.utils.machine_id.get_machine_id_file", return_value=machine_id_file),
            patch("gobby.utils.machine_id._generate_machine_id", return_value="fresh-id"),
        ):
            assert _get_or_create_machine_id() == "fresh-id"

        assert machine_id_file.read_text() == "fresh-id"
        assert (tmp_path / ".machine_id.lock").exists()
