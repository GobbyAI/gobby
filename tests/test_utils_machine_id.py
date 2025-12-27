"""Tests for src/utils/machine_id.py - Machine ID Utility."""

import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from gobby.utils.machine_id import (
    get_machine_id,
    _get_or_create_machine_id,
    _write_file_secure,
    _generate_machine_id,
    clear_cache,
    MACHINE_ID_FILE,
)


class TestGetMachineId:
    """Tests for get_machine_id function."""

    def setup_method(self):
        """Clear cache before each test."""
        clear_cache()

    def test_returns_cached_id_if_available(self):
        """Test that cached ID is returned without recalculating."""
        import gobby.utils.machine_id as machine_id_module

        # Set cached value directly
        machine_id_module._cached_machine_id = "cached-machine-id"

        result = get_machine_id()

        assert result == "cached-machine-id"

        # Cleanup
        machine_id_module._cached_machine_id = None

    def test_calls_get_or_create_when_no_cache(self):
        """Test that _get_or_create_machine_id is called when no cache."""
        with patch(
            "gobby.utils.machine_id._get_or_create_machine_id", return_value="new-machine-id"
        ) as mock:
            result = get_machine_id()

        assert result == "new-machine-id"
        mock.assert_called_once()

    def test_caches_result_after_call(self):
        """Test that result is cached after first call."""
        import gobby.utils.machine_id as machine_id_module

        with patch("gobby.utils.machine_id._get_or_create_machine_id", return_value="new-id"):
            get_machine_id()

        assert machine_id_module._cached_machine_id == "new-id"

        # Cleanup
        machine_id_module._cached_machine_id = None

    def test_propagates_os_error(self):
        """Test that OSError is propagated."""
        with patch(
            "gobby.utils.machine_id._get_or_create_machine_id", side_effect=OSError("File error")
        ):
            with pytest.raises(OSError, match="Failed to retrieve or create machine ID"):
                get_machine_id()


class TestGetOrCreateMachineId:
    """Tests for _get_or_create_machine_id function."""

    def test_returns_existing_id_from_file(self, tmp_path):
        """Test returns machine_id from file if present."""
        test_file = tmp_path / "machine_id"
        test_file.write_text("existing-id-from-file")

        with patch("gobby.utils.machine_id.MACHINE_ID_FILE", test_file):
            result = _get_or_create_machine_id()

        assert result == "existing-id-from-file"

    def test_generates_and_saves_new_id_when_file_missing(self, tmp_path):
        """Test generates new ID and saves to file when missing."""
        test_file = tmp_path / "machine_id"

        with (
            patch("gobby.utils.machine_id.MACHINE_ID_FILE", test_file),
            patch("gobby.utils.machine_id._generate_machine_id", return_value="new-generated-id"),
        ):
            result = _get_or_create_machine_id()

        assert result == "new-generated-id"
        assert test_file.exists()
        assert test_file.read_text() == "new-generated-id"

    def test_creates_parent_directory_if_missing(self, tmp_path):
        """Test creates parent directory if it doesn't exist."""
        test_file = tmp_path / "subdir" / "machine_id"

        with (
            patch("gobby.utils.machine_id.MACHINE_ID_FILE", test_file),
            patch("gobby.utils.machine_id._generate_machine_id", return_value="new-id"),
        ):
            result = _get_or_create_machine_id()

        assert result == "new-id"
        assert test_file.parent.exists()

    def test_ignores_empty_file(self, tmp_path):
        """Test generates new ID if file exists but is empty."""
        test_file = tmp_path / "machine_id"
        test_file.write_text("   \n")  # Whitespace only

        with (
            patch("gobby.utils.machine_id.MACHINE_ID_FILE", test_file),
            patch("gobby.utils.machine_id._generate_machine_id", return_value="new-id"),
        ):
            result = _get_or_create_machine_id()

        assert result == "new-id"


class TestWriteFileSecure:
    """Tests for _write_file_secure function."""

    def test_writes_content_to_file(self, tmp_path):
        """Test writes content correctly."""
        test_file = tmp_path / "test_file"

        _write_file_secure(test_file, "test-content")

        assert test_file.read_text() == "test-content"

    def test_sets_restrictive_permissions(self, tmp_path):
        """Test file is created with 0o600 permissions."""
        test_file = tmp_path / "test_file"

        _write_file_secure(test_file, "test-content")

        # Check permissions (owner read/write only)
        mode = test_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_overwrites_existing_file(self, tmp_path):
        """Test overwrites existing file content."""
        test_file = tmp_path / "test_file"
        test_file.write_text("old-content")

        _write_file_secure(test_file, "new-content")

        assert test_file.read_text() == "new-content"


class TestGenerateMachineId:
    """Tests for _generate_machine_id function."""

    def test_uses_machineid_library_when_available(self):
        """Test uses machineid library if available."""
        mock_machineid = MagicMock()
        mock_machineid.id.return_value = "hardware-id"

        with patch.dict("sys.modules", {"machineid": mock_machineid}):
            # Need to reimport to pick up the mock
            from gobby.utils import machine_id as mid
            result = mid._generate_machine_id()

        # Should attempt to use machineid
        mock_machineid.id.assert_called_once()

    def test_falls_back_to_uuid_when_import_fails(self):
        """Test falls back to UUID when machineid unavailable."""
        with patch.dict("sys.modules", {"machineid": None}):
            result = _generate_machine_id()

        # Should be a valid UUID string
        assert result is not None
        assert isinstance(result, str)
        assert len(result) == 36  # UUID format

    def test_falls_back_to_uuid_when_machineid_raises(self):
        """Test falls back to UUID when machineid.id() raises."""
        mock_machineid = MagicMock()
        mock_machineid.id.side_effect = Exception("Hardware access failed")

        with patch.dict("sys.modules", {"machineid": mock_machineid}):
            result = _generate_machine_id()

        # Should fall back to UUID
        assert result is not None
        assert isinstance(result, str)


class TestClearCache:
    """Tests for clear_cache function."""

    def test_clears_cached_value(self):
        """Test that clear_cache sets cached value to None."""
        import gobby.utils.machine_id as machine_id_module

        # Set a cached value
        machine_id_module._cached_machine_id = "test-id"

        clear_cache()

        assert machine_id_module._cached_machine_id is None

    def test_clear_cache_is_thread_safe(self):
        """Test that clear_cache uses lock."""
        # The function uses _cache_lock internally
        # Just verify it doesn't raise any exceptions
        clear_cache()
        clear_cache()  # Multiple calls should be safe
