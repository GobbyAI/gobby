from __future__ import annotations

import threading
from collections.abc import Awaitable
from unittest.mock import MagicMock

import pytest

from gobby.communications.webhook_verification import verify_webhook_with_timeout


async def test_verify_webhook_with_timeout_offloads_sync_adapter_and_sets_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.get_ident()
    worker_threads: list[int] = []
    timeouts: list[float] = []
    adapter = MagicMock()

    def verify_webhook(_payload: bytes, _headers: dict[str, str], _secret: str) -> bool:
        worker_threads.append(threading.get_ident())
        return True

    async def wait_for(awaitable: Awaitable[bool], timeout: float | None = None) -> bool:
        timeouts.append(timeout or 0.0)
        return await awaitable

    adapter.verify_webhook.side_effect = verify_webhook
    monkeypatch.setattr("gobby.communications.webhook_verification.asyncio.wait_for", wait_for)

    assert await verify_webhook_with_timeout(adapter, b"{}", {}, "secret", 1.5) is True
    assert timeouts == [1.5]
    assert len(worker_threads) == 1
    assert worker_threads[0] != loop_thread
