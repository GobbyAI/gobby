"""Red tests for removing deprecated session-manager names."""

from __future__ import annotations

from importlib import import_module

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

_DEPRECATED_ALIAS = "Local" + "SessionManager"
_LEGACY_MANAGER_MODULE = ".".join(("gobby", "sessions", "manager"))


def test_legacy_sessions_manager_module_is_removed() -> None:
    """The old session-manager shim should be deleted in phase 4."""
    with pytest.raises(ModuleNotFoundError):
        import_module(_LEGACY_MANAGER_MODULE)


def test_storage_packages_do_not_export_local_session_manager_name() -> None:
    """Public storage packages should expose only SessionManager."""
    storage_module = import_module("gobby.storage")
    sessions_module = import_module("gobby.storage.sessions")

    assert getattr(storage_module, _DEPRECATED_ALIAS, None) is None
    assert getattr(sessions_module, _DEPRECATED_ALIAS, None) is None


def test_session_manager_no_longer_exposes_get_session_shim(
    temp_db: HubDatabase,
) -> None:
    """Callers should use attribute-based Session objects via SessionManager.get()."""
    session_manager = SessionManager(temp_db)

    assert not hasattr(session_manager, "get_session")
