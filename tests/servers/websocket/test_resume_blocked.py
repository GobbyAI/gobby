from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.websocket.handlers.session_observe_continue import check_resume_blocked

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_resume_db_checks_use_websocket_db_executor() -> None:
    db = MagicMock()
    mixin = SimpleNamespace(
        session_manager=SimpleNamespace(db=db),
        _chat_sessions={},
    )
    source_session = SimpleNamespace(id="session-1")

    with patch(
        "gobby.servers.websocket.handlers.session_observe_continue.run_db",
        new_callable=AsyncMock,
        side_effect=[None, None],
    ) as run_db:
        reason = await check_resume_blocked(mixin, source_session)

    assert reason is None
    assert run_db.await_count == 2
    agent_check, pipeline_check = run_db.await_args_list
    assert agent_check.args[0:2] == (mixin, db.fetchone)
    assert "FROM agent_runs" in agent_check.args[2]
    assert pipeline_check.args[0:2] == (mixin, db.fetchone)
    assert "FROM pipeline_executions" in pipeline_check.args[2]
