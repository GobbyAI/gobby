"""Tests for pinned overlay file sync retirement in code_index.sync_worker."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from gobby.code_index import sync_worker
from gobby.code_index._storage.files import PinnedFileSync
from gobby.code_index.gcode_gateway import GcodeGateway, GcodeTimeoutError
from gobby.code_index.models import IndexedFile
from gobby.code_index.sync_worker import _sync_pinned_file
from gobby.config.code_index import CodeIndexConfig
from tests.code_index.conftest import PROJECT_ID

pytestmark = pytest.mark.unit

PINNED_FILE = "tests/e2e/test_devteam_lifecycle.py"
PINNED_ID = "00000000-0000-4000-8000-00000000dddd"


def _pin() -> PinnedFileSync:
    return PinnedFileSync(
        file=IndexedFile(
            id=PINNED_ID,
            project_id=PROJECT_ID,
            file_path=PINNED_FILE,
            language="",
            content_hash="a" * 64,
        ),
        graph=False,
        vectors=True,
    )


async def _sync_pin(gateway: object, project_root: Path) -> None:
    await _sync_pinned_file(
        gcode_gateway=cast(GcodeGateway, gateway),
        config=CodeIndexConfig(embedding_enabled=True, graph_enabled=False),
        project_root=project_root,
        pin=_pin(),
        vector_breaker=None,
        gateway_breaker=None,
    )


@pytest.mark.asyncio
async def test_pinned_source_missing_retires_with_one_info_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A pin whose source left the checkout is retired with one INFO line and no traceback."""
    gateway = SimpleNamespace(
        vector_sync_file=AsyncMock(
            return_value={
                "success": True,
                "status": "skipped",
                "reason": "source_missing",
                "file_path": PINNED_FILE,
            }
        ),
        graph_sync_file=AsyncMock(),
    )
    caplog.set_level(logging.INFO, logger=sync_worker.logger.name)

    await _sync_pin(gateway, tmp_path)

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    retired = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(retired) == 1
    assert PINNED_FILE in retired[0]
    gateway.vector_sync_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_pinned_transient_vector_failure_still_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient gcode timeout retries the pinned vector sync until it succeeds."""
    monkeypatch.setattr(sync_worker, "_VECTOR_SYNC_RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    transient = GcodeTimeoutError("gcode timed out")
    gateway = SimpleNamespace(
        vector_sync_file=AsyncMock(side_effect=[transient, transient, {"success": True}]),
        graph_sync_file=AsyncMock(),
    )
    caplog.set_level(logging.INFO, logger=sync_worker.logger.name)

    await _sync_pin(gateway, tmp_path)

    assert gateway.vector_sync_file.await_count == 3
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
