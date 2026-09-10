"""An interrupted lifecycle request must not strand handoff admission."""

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from gobby.servers.http import HTTPServer
from gobby.servers.routes.admin._lifecycle import _prepare_handoff_shutdown
from gobby.sessions.handoff_shutdown import lock_handoff_staging
from gobby.storage.hub.protocol import HubDatabase


async def test_cancelled_request_reopens_admission_after_db_check(temp_db: HubDatabase) -> None:
    entered = asyncio.Event()
    finish = asyncio.Event()
    machine_id = str(uuid4())

    async def run_db(
        func: Callable[[HubDatabase, str], None], db: HubDatabase, machine: str
    ) -> None:
        entered.set()
        await finish.wait()
        func(db, machine)

    server = cast(
        HTTPServer,
        SimpleNamespace(session_manager=SimpleNamespace(db=temp_db), run_db=run_db),
    )
    pending = asyncio.create_task(_prepare_handoff_shutdown(server, machine_id))
    await asyncio.wait_for(entered.wait(), timeout=5)
    pending.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    with temp_db.transaction() as conn:
        lock_handoff_staging(conn, machine_id)
