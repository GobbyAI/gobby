"""reload_cache surfaces bundled sync failures to callers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.workflows._import import reload_cache

pytestmark = pytest.mark.unit


def test_reload_cache_surfaces_bundled_sync_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = MagicMock()
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.workflows._import.sync_imported_workflows",
        lambda *_args, **_kwargs: {"synced": 0, "errors": []},
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.workflows._import.sync_bundled_content_to_db",
        lambda *_args, **_kwargs: {"details": {}, "errors": ["agents: boom"]},
    )

    result = reload_cache(loader, db=object())

    assert result["success"] is True
    assert result["bundled_sync_errors"] == ["agents: boom"]
    loader.clear_cache.assert_called_once_with()
