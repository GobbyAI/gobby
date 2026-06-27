"""Focused regression tests for managed provider backend helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.websocket.chat.backends.base import ProviderBackendHealth
from gobby.servers.websocket.chat.backends.codex import (
    CodexManagedChatSession,
    CodexWebChatBackend,
)
from gobby.servers.websocket.chat.backends.qwen import (
    QwenManagedChatSession,
    QwenWebChatBackend,
)
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit


def test_codex_backend_and_session_share_provider_id() -> None:
    backend = CodexWebChatBackend(client=None)
    session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)

    assert session.provider == backend.provider == "codex"


@pytest.mark.asyncio
async def test_dispatch_before_tool_once_shares_inflight_none_response() -> None:
    session = CodexManagedChatSession(conversation_id="conv-codex", _backend=MagicMock())
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_pre_tool(_tool_name: str, _tool_input: dict[str, object]) -> None:
        started.set()
        await release.wait()
        return None

    session._apply_pre_tool_lifecycle = AsyncMock(side_effect=fake_pre_tool)

    first = asyncio.create_task(session._dispatch_before_tool_once("same-tool", "Read", {}))
    await started.wait()
    second = asyncio.create_task(session._dispatch_before_tool_once("same-tool", "Read", {}))
    await drain_asyncio_tasks()

    assert session._apply_pre_tool_lifecycle.await_count == 1

    release.set()
    assert await first is None
    assert await second is None
    assert "same-tool" in session._before_tool_cached_responses

    third = await session._dispatch_before_tool_once("same-tool", "Read", {})
    assert third is None
    assert session._apply_pre_tool_lifecycle.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resume_session_id, session_capabilities, agent_capabilities, expected_request",
    [
        (None, {}, {}, "create"),
        ("prev", {"resume": True}, {"loadSession": True}, "resume"),
        ("prev", {}, {"loadSession": True}, "load"),
        ("prev", {}, {}, "create"),
    ],
)
async def test_acp_attach_session_resolves_cwd_and_seeds_trust_before_session_request(
    temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    resume_session_id: str | None,
    session_capabilities: dict[str, bool],
    agent_capabilities: dict[str, bool],
    expected_request: str,
) -> None:
    monkeypatch.chdir(temp_dir)
    workspace = temp_dir / "workspace"
    workspace.mkdir()
    expected_cwd = str(workspace.resolve())
    order: list[str] = []

    async def create_session(**_kwargs: object) -> dict[str, str]:
        order.append("create")
        return {"sessionId": "created"}

    async def load_session(_session_id: str, **_kwargs: object) -> dict[str, str]:
        order.append("load")
        return {"sessionId": "loaded"}

    async def resume_session(_session_id: str, **_kwargs: object) -> dict[str, str]:
        order.append("resume")
        return {"sessionId": "resumed"}

    def record_trust(_provider: str, _cwd: str) -> None:
        order.append("trust")

    client = MagicMock()
    client.is_started = True
    client.session_id = None
    client.session_capabilities = session_capabilities
    client.agent_capabilities = agent_capabilities
    client.create_session = AsyncMock(side_effect=create_session)
    client.load_session = AsyncMock(side_effect=load_session)
    client.resume_session = AsyncMock(side_effect=resume_session)

    backend = QwenWebChatBackend(client=client, default_model="qwen-default")
    backend._health = ProviderBackendHealth(provider="qwen", available=True)
    session = QwenManagedChatSession(conversation_id="conv-qwen", _backend=backend)
    session.project_path = "workspace"
    session.resume_session_id = resume_session_id

    with patch(
        "gobby.servers.websocket.chat.backends.acp.pre_approve_directory",
        side_effect=record_trust,
    ) as pre_approve:
        await backend.attach_session(session)

    pre_approve.assert_called_once_with("qwen", expected_cwd)
    assert order == ["trust", expected_request]
    expected_ids = {"create": "created", "load": "loaded", "resume": "resumed"}
    assert session.sdk_session_id == expected_ids[expected_request]
    if expected_request == "create":
        client.create_session.assert_awaited_once_with(
            model="qwen-default",
            cwd=expected_cwd,
            reasoning_effort=None,
        )
        client.load_session.assert_not_awaited()
        client.resume_session.assert_not_awaited()
    elif expected_request == "load":
        client.load_session.assert_awaited_once_with(
            "prev",
            model="qwen-default",
            cwd=expected_cwd,
            reasoning_effort=None,
        )
        client.create_session.assert_not_awaited()
        client.resume_session.assert_not_awaited()
    else:
        client.resume_session.assert_awaited_once_with(
            "prev",
            model="qwen-default",
            cwd=expected_cwd,
            reasoning_effort=None,
        )
        client.create_session.assert_not_awaited()
        client.load_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_acp_attach_session_requires_resolved_model_before_session_request() -> None:
    client = MagicMock()
    client.is_started = True
    client.session_id = None
    client.create_session = AsyncMock()
    client.load_session = AsyncMock()

    backend = QwenWebChatBackend(client=client, default_model=None)
    backend._health = ProviderBackendHealth(provider="qwen", available=True)
    session = QwenManagedChatSession(conversation_id="conv-qwen", _backend=backend)

    with pytest.raises(RuntimeError, match="model could not be resolved"):
        await backend.attach_session(session)

    client.create_session.assert_not_awaited()
    client.load_session.assert_not_awaited()
