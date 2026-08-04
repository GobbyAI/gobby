"""Tests for managed credential leases around ToolChat requests."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from gobby.ai._managed_tool_chat_lease import build_managed_tool_chat_lease_factory
from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolPolicy

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_lease_replaces_client_path_and_revokes_after_success() -> None:
    session_id = uuid4()
    execution_id = uuid4()
    bootstrap_path = Path("/private/runtime/managed/bootstrap.json")
    manager = MagicMock()
    manager.issue_tool_request.return_value = SimpleNamespace(
        project_path="/authoritative/repo",
        credential=SimpleNamespace(
            managed_execution_id=execution_id,
            bootstrap_path=bootstrap_path,
        ),
    )
    factory = build_managed_tool_chat_lease_factory(manager)
    request = ToolChatRequest(
        prompt="Inspect auth.",
        tool_policy=ToolPolicy(cli="gcode", tools=("search",)),
        project_path="/client/spoof",
        session_id=session_id,
    )

    async with factory(request, 30.0) as scoped:
        assert scoped.project_path == "/authoritative/repo"
        assert scoped.managed_execution_id == execution_id
        assert scoped.credential_bootstrap_path == str(bootstrap_path)

    manager.issue_tool_request.assert_called_once()
    manager.revoke.assert_called_once_with(
        execution_id,
        reason="tool-request-finally",
    )


@pytest.mark.asyncio
async def test_lease_revokes_credential_if_cancelled_during_issuance() -> None:
    session_id = uuid4()
    execution_id = uuid4()
    issue_started = threading.Event()
    allow_issue = threading.Event()
    issue_finished = threading.Event()
    manager = MagicMock()

    def issue_tool_request(**_kwargs: object) -> SimpleNamespace:
        issue_started.set()
        assert allow_issue.wait(timeout=5)
        issue_finished.set()
        return SimpleNamespace(
            project_path="/authoritative/repo",
            credential=SimpleNamespace(
                managed_execution_id=execution_id,
                bootstrap_path=Path("/private/runtime/managed/bootstrap.json"),
            ),
        )

    manager.issue_tool_request.side_effect = issue_tool_request
    factory = build_managed_tool_chat_lease_factory(manager)
    request = ToolChatRequest(
        prompt="Inspect auth.",
        tool_policy=ToolPolicy(cli="gcode", tools=("search",)),
        project_path="/client/spoof",
        session_id=session_id,
    )

    async def consume_lease() -> None:
        async with factory(request, 30.0):
            pytest.fail("cancelled request must not enter the lease body")

    task = asyncio.create_task(consume_lease())
    assert await asyncio.to_thread(issue_started.wait, 5)
    task.cancel()
    allow_issue.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(issue_finished.wait, 5)
    manager.revoke.assert_called_once_with(
        execution_id,
        reason="tool-request-finally",
    )
