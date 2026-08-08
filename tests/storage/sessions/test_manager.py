"""Tests for the unified storage SessionManager surface."""

from __future__ import annotations

from collections.abc import Iterator
from importlib import import_module
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "20000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _storage_session_manager_cls():
    module = import_module("gobby.storage.sessions")
    manager_cls = getattr(module, "SessionManager", None)
    assert manager_cls is not None, "gobby.storage.sessions must export SessionManager"
    return manager_cls


@pytest.fixture
def project_id(temp_db: HubDatabase) -> str:
    """Create a project and return its ID."""
    return LocalProjectManager(temp_db).create(name="test-project", repo_path="/tmp/test").id


def test_storage_package_exports_internal_session_manager() -> None:
    public_module = import_module("gobby.storage.sessions")
    internal_module = import_module("gobby.storage.sessions._manager")
    session_manager_cls = getattr(public_module, "SessionManager", None)

    assert session_manager_cls is not None
    assert internal_module.SessionManager is session_manager_cls


def test_update_session_status_returns_bool(temp_db: HubDatabase, project_id: str) -> None:
    session_mgr = _storage_session_manager_cls()(temp_db)
    session_id = session_mgr.register_session(
        external_id="status-session",
        machine_id="20000000-0000-4000-8000-000000000002",
        source="claude",
        project_id=project_id,
    )

    assert session_mgr.update_session_status(session_id, "paused") is True
    assert session_mgr.update_session_status("missing-session", "paused") is False
