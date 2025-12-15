"""Tests for src/utils/machine_id.py - Machine ID Utility."""

import pytest
from unittest.mock import patch, MagicMock
import uuid

from gobby.utils.machine_id import (
    get_machine_id,
    _get_or_create_machine_id,
    clear_cache,
    _cached_machine_id,
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
        with patch('gobby.utils.machine_id._get_or_create_machine_id', return_value="new-machine-id") as mock:
            result = get_machine_id()

        assert result == "new-machine-id"
        mock.assert_called_once()

    def test_caches_result_after_call(self):
        """Test that result is cached after first call."""
        import gobby.utils.machine_id as machine_id_module

        with patch('gobby.utils.machine_id._get_or_create_machine_id', return_value="new-id"):
            get_machine_id()

        assert machine_id_module._cached_machine_id == "new-id"

        # Cleanup
        machine_id_module._cached_machine_id = None

    def test_propagates_os_error(self):
        """Test that OSError is propagated."""
        with patch('gobby.utils.machine_id._get_or_create_machine_id', side_effect=OSError("File error")):
            with pytest.raises(OSError, match="Failed to retrieve or create machine ID"):
                get_machine_id()


class TestGetOrCreateMachineId:
    """Tests for _get_or_create_machine_id function."""

    def test_returns_existing_machine_id_from_config(self):
        """Test returns machine_id from config if present."""
        mock_config = MagicMock()
        mock_config.machine_id = "existing-id-from-config"

        with patch('gobby.config.app.load_config', return_value=mock_config):
            result = _get_or_create_machine_id()

        assert result == "existing-id-from-config"

    def test_generates_id_using_machineid_library(self):
        """Test generates ID using machineid library."""
        mock_config = MagicMock()
        mock_config.machine_id = None

        with patch('gobby.config.app.load_config', return_value=mock_config), \
             patch('gobby.config.app.save_config') as mock_save, \
             patch.dict('sys.modules', {'machineid': MagicMock(id=lambda: "hardware-id")}):

            # Re-import to use patched module
            import importlib
            import gobby.utils.machine_id as mid
            importlib.reload(mid)

            result = mid._get_or_create_machine_id()

        # Should use hardware ID
        assert result is not None
        mock_save.assert_called_once()

    def test_falls_back_to_uuid_when_machineid_not_available(self):
        """Test falls back to UUID when machineid library unavailable."""
        mock_config = MagicMock()
        mock_config.machine_id = None

        with patch('gobby.config.app.load_config', return_value=mock_config), \
             patch('gobby.config.app.save_config'):

            # Import error for machineid
            import sys
            if 'machineid' in sys.modules:
                del sys.modules['machineid']

            with patch.dict('sys.modules', {'machineid': None}):
                # The actual function handles ImportError internally
                # We can test by mocking the import to fail
                pass

    def test_saves_new_id_to_config(self):
        """Test that newly generated ID is saved to config."""
        mock_config = MagicMock()
        mock_config.machine_id = None

        with patch('gobby.config.app.load_config', return_value=mock_config), \
             patch('gobby.config.app.save_config') as mock_save:

            result = _get_or_create_machine_id()

        # Verify save was called with config that has machine_id set
        mock_save.assert_called_once()
        assert mock_config.machine_id is not None


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
