"""Background cleanup must not initialize services that never started."""

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from gobby.runner_background_services import shutdown_background_services
from gobby.tasks import transcript_evidence_pool

pytestmark = pytest.mark.unit


def test_shutdown_without_embedding_leases_does_not_import_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "gobby.runner_init.embedding_lease", raising=False)
    # Startup is unavailable: shutdown must still stop its already-running workers.
    monkeypatch.setitem(sys.modules, "gobby.runner_init", None)
    stop_pool = MagicMock()
    monkeypatch.setattr(transcript_evidence_pool, "shutdown_transcript_evidence_pool", stop_pool)

    shutdown_background_services()

    stop_pool.assert_called_once_with()
    assert "gobby.runner_init.embedding_lease" not in sys.modules


def test_shutdown_disposes_loaded_embedding_leases_before_stopping_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    lease_module = ModuleType("gobby.runner_init.embedding_lease")

    def dispose() -> None:
        calls.append("leases")

    def stop_pool() -> None:
        calls.append("pool")

    monkeypatch.setattr(lease_module, "dispose_active_embedding_leases", dispose, raising=False)
    monkeypatch.setitem(sys.modules, "gobby.runner_init.embedding_lease", lease_module)
    monkeypatch.setattr(transcript_evidence_pool, "shutdown_transcript_evidence_pool", stop_pool)

    shutdown_background_services()

    assert calls == ["leases", "pool"]
