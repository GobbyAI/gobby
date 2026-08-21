"""Cross-backend contract for ChatSessionProtocol.clear_context (#20549)."""

from __future__ import annotations

import inspect
import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.chat_session import ChatSession
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.websocket.chat.backends.acp import ACPWebChatBackend
from gobby.servers.websocket.chat.backends.acp_session import ACPManagedChatSession
from gobby.servers.websocket.chat.backends.base import (
    ManagedChatSessionBase,
    ProviderBackendHealth,
)
from gobby.servers.websocket.chat.backends.codex import (
    CodexManagedChatSession,
    CodexWebChatBackend,
)
from gobby.servers.websocket.chat.backends.droid import (
    DroidManagedChatSession,
    DroidWebChatBackend,
)
from gobby.servers.websocket.chat.backends.grok import (
    GrokManagedChatSession,
    GrokWebChatBackend,
)
from gobby.servers.websocket.chat.backends.qwen import (
    QwenManagedChatSession,
    QwenWebChatBackend,
)

pytestmark = pytest.mark.unit

_BACKENDS = ("claude", "codex", "acp", "droid", "grok", "qwen")


def _continuation_ids(session: object) -> set[str]:
    ids: set[str] = set()
    for attr in ("resume_session_id", "sdk_session_id", "_thread_id"):
        value = getattr(session, attr, None)
        if isinstance(value, str) and value:
            ids.add(value)
    return ids


class _AcpContractBackend(ACPWebChatBackend):
    provider = "acp"
    display_name = "ACP"
    acp_client_cls = MagicMock


class _FakeDroidStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeDroidStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [(line + "\n").encode("utf-8") for line in lines]

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeDroidProcess:
    def __init__(self, session_id: str) -> None:
        self.stdin = _FakeDroidStdin()
        self.stdout = _FakeDroidStdout([_droid_init_line(session_id)])
        self.stderr = None
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _droid_init_line(session_id: str) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "type": "response",
            "factoryApiVersion": "1.0.0",
            "factoryProtocolVersion": "1.25.0",
            "id": "gobby-init-1",
            "result": {
                "sessionId": session_id,
                "session": {"messages": []},
                "settings": {"modelId": "gpt-5.4", "reasoningEffort": "high"},
            },
        }
    )


def _fake_acp_client(*, new_id: str) -> MagicMock:
    client = MagicMock()
    client.is_started = True
    client.session_id = None
    client.session_capabilities = {"resume": True}
    client.agent_capabilities = {"loadSession": True}
    client.create_session = AsyncMock(return_value={"sessionId": new_id})
    client.resume_session = AsyncMock(return_value={"sessionId": "resumed-should-not-use"})
    client.load_session = AsyncMock(return_value={"sessionId": "loaded-should-not-use"})
    return client


def test_protocol_declares_clear_context() -> None:
    assert hasattr(ChatSessionProtocol, "clear_context")
    method = ChatSessionProtocol.clear_context
    assert inspect.iscoroutinefunction(method)
    signature = inspect.signature(method)
    assert list(signature.parameters) == ["self"]
    assert signature.return_annotation in {bool, "bool"}


def test_managed_base_exposes_restart_default() -> None:
    assert inspect.iscoroutinefunction(ManagedChatSessionBase.clear_context)
    signature = inspect.signature(ManagedChatSessionBase.clear_context)
    assert list(signature.parameters) == ["self"]
    assert signature.return_annotation in {bool, "bool"}


def test_six_backends_satisfy_protocol_once_clear_context_exists() -> None:
    sessions: list[ChatSessionProtocol] = [
        ChatSession(conversation_id="claude"),
        CodexManagedChatSession(conversation_id="codex", _backend=MagicMock()),
        ACPManagedChatSession(conversation_id="acp", chat_mode="plan"),
        DroidManagedChatSession(conversation_id="droid", _backend=MagicMock()),
        GrokManagedChatSession(conversation_id="grok", _backend=MagicMock()),
        QwenManagedChatSession(conversation_id="qwen", _backend=MagicMock()),
    ]
    assert [session.provider for session in sessions] == list(_BACKENDS)
    for session in sessions:
        assert isinstance(session, ChatSessionProtocol)
        assert inspect.iscoroutinefunction(session.clear_context)


@pytest.mark.asyncio
async def test_managed_default_resets_identifiers_before_start() -> None:
    order: list[str] = []

    class _Backend:
        async def detach_session(self, session: ManagedChatSessionBase) -> None:
            order.append("stop")
            session._connected = False

        async def attach_session(
            self,
            session: ManagedChatSessionBase,
            *,
            model: str | None = None,
        ) -> None:
            order.append("start")
            if session.sdk_session_id or session.resume_session_id:
                order.append("resumed")
                session.sdk_session_id = session.sdk_session_id or session.resume_session_id
            else:
                order.append("fresh")
                session.sdk_session_id = "fresh-sdk"
            session._model = model
            session._connected = True

    session = ManagedChatSessionBase(
        conversation_id="conv-default",
        provider="acp",
        chat_mode="normal",
        _backend=_Backend(),
    )
    session._model = "kept-model"
    session.sdk_session_id = "old-sdk"
    session.resume_session_id = "old-resume"

    result = await session.clear_context()

    assert result is True
    assert order == ["stop", "start", "fresh"]
    assert session.sdk_session_id == "fresh-sdk"
    assert session.resume_session_id is None
    assert session.model == "kept-model"
    assert session.chat_mode == "normal"


@pytest.mark.asyncio
async def test_managed_default_returns_false_when_start_fails() -> None:
    class _Backend:
        async def detach_session(self, session: ManagedChatSessionBase) -> None:
            session._connected = False

        async def attach_session(
            self,
            session: ManagedChatSessionBase,
            *,
            model: str | None = None,
        ) -> None:
            del model
            raise RuntimeError("attach exploded")

    session = ManagedChatSessionBase(
        conversation_id="conv-fail",
        provider="droid",
        chat_mode="plan",
        _backend=_Backend(),
    )
    session.sdk_session_id = "old-sdk"
    session.resume_session_id = "old-resume"

    result = await session.clear_context()

    assert result is False
    assert session.resume_session_id is None
    assert session.sdk_session_id is None


@pytest.mark.asyncio
async def test_claude_native_drops_sdk_resume_identifiers() -> None:
    session = ChatSession(conversation_id="claude-clear")
    session._model = "sonnet"
    session.chat_mode = "normal"
    session.resume_session_id = "claude-resume-old"
    session.sdk_session_id = "claude-sdk-old"
    session._connected = True
    session._client = AsyncMock()
    old_ids = _continuation_ids(session)
    captured_options: list[dict[str, Any]] = []

    def capture_options(**kwargs: Any) -> MagicMock:
        captured_options.append(dict(kwargs))
        return MagicMock()

    with (
        patch("gobby.servers.chat_session._find_cli_path", return_value="/usr/bin/claude"),
        patch(
            "gobby.servers.chat_session._build_gobby_mcp_entry",
            return_value={"command": "gobby", "args": ["mcp-server"]},
        ),
        patch("gobby.servers.chat_session._find_project_root", return_value=None),
        patch("gobby.servers.chat_session.ClaudeAgentOptions", side_effect=capture_options),
        patch("gobby.servers.chat_session.ClaudeSDKClient") as mock_client_cls,
    ):
        mock_client_cls.return_value = AsyncMock()
        result = await session.clear_context()

    assert result is True
    assert session.chat_mode == "normal"
    assert session.model == "sonnet"
    assert session.resume_session_id is None
    assert _continuation_ids(session).isdisjoint(old_ids)
    assert captured_options
    assert captured_options[-1]["resume"] is None
    assert captured_options[-1]["continue_conversation"] is False
    assert captured_options[-1]["permission_mode"] == "default"
    assert captured_options[-1]["model"] == "sonnet"


@pytest.mark.asyncio
async def test_codex_clear_context_archives_and_starts_fresh_thread() -> None:
    fake_client = SimpleNamespace(
        archive_thread=AsyncMock(),
        start_thread=AsyncMock(return_value=SimpleNamespace(id="codex-new", path=None)),
        resume_thread=AsyncMock(return_value=SimpleNamespace(id="codex-resumed", path=None)),
        is_connected=True,
    )
    backend = CodexWebChatBackend(client=fake_client)  # type: ignore[arg-type]
    backend._health = ProviderBackendHealth(provider="codex", available=True)
    session = CodexManagedChatSession(conversation_id="codex-clear", _backend=backend)
    session._model = "gpt-5.6-sol"
    session.chat_mode = "normal"
    session._thread_id = "codex-old"
    session.sdk_session_id = "codex-old"
    session.resume_session_id = "codex-resume"
    backend._sessions_by_thread["codex-old"] = session
    old_ids = _continuation_ids(session)

    result = await session.clear_context()

    assert result is True
    fake_client.archive_thread.assert_awaited_once_with("codex-old")
    fake_client.resume_thread.assert_not_awaited()
    fake_client.start_thread.assert_awaited_once()
    assert "codex-old" not in backend._sessions_by_thread
    assert session._thread_id == "codex-new"
    assert session.sdk_session_id == "codex-new"
    assert session.resume_session_id is None
    assert session.model == "gpt-5.6-sol"
    assert session.chat_mode == "normal"
    assert _continuation_ids(session).isdisjoint(old_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["acp", "grok", "qwen"])
async def test_acp_family_clear_context_creates_session_instead_of_resume(
    provider: str,
) -> None:
    client = _fake_acp_client(new_id=f"{provider}-new")
    if provider == "acp":
        backend: ACPWebChatBackend = _AcpContractBackend(client=client, default_model="acp-model")
        session = ACPManagedChatSession(
            conversation_id="acp-clear",
            chat_mode="plan",
            _backend=backend,
        )
    elif provider == "grok":
        backend = GrokWebChatBackend(client=client, default_model="grok-model")
        session = GrokManagedChatSession(conversation_id="grok-clear", _backend=backend)
    else:
        backend = QwenWebChatBackend(client=client, default_model="qwen-model")
        session = QwenManagedChatSession(conversation_id="qwen-clear", _backend=backend)
    backend._health = ProviderBackendHealth(provider=provider, available=True)
    session._model = f"{provider}-model"
    session.chat_mode = "normal"
    session.sdk_session_id = f"{provider}-old"
    session.resume_session_id = f"{provider}-resume"
    old_ids = _continuation_ids(session)
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "gobby.servers.websocket.chat.backends.acp.pre_approve_directory",
                return_value=None,
            )
        )
        if provider == "qwen":
            stack.enter_context(
                patch(
                    "gobby.servers.websocket.chat.backends.qwen.ensure_qwen_local_openai_model_ready",
                    new_callable=AsyncMock,
                )
            )
        result = await session.clear_context()

    assert result is True
    client.resume_session.assert_not_awaited()
    client.load_session.assert_not_awaited()
    client.create_session.assert_awaited_once()
    assert session.sdk_session_id == f"{provider}-new"
    assert session.resume_session_id is None
    assert session.chat_mode == "normal"
    assert session.model == f"{provider}-model"
    assert _continuation_ids(session).isdisjoint(old_ids)


@pytest.mark.asyncio
async def test_droid_clear_context_starts_process_without_old_session_id(
    tmp_path: Path,
) -> None:
    processes = [
        _FakeDroidProcess("droid-old"),
        _FakeDroidProcess("droid-new"),
    ]
    backend = DroidWebChatBackend()
    session = DroidManagedChatSession(conversation_id="droid-clear", _backend=backend)
    session.project_path = str(tmp_path)
    session.chat_mode = "normal"

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which",
            return_value="/bin/droid",
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            side_effect=processes,
        ),
    ):
        await backend.attach_session(session, model="gpt-5.4")
        old_ids = _continuation_ids(session)
        assert session.sdk_session_id == "droid-old"
        result = await session.clear_context()

    assert result is True
    first_init = json.loads(processes[0].stdin.writes[0].decode("utf-8"))
    second_init = json.loads(processes[1].stdin.writes[0].decode("utf-8"))
    assert first_init["method"] == "droid.initialize_session"
    assert "sessionId" not in first_init["params"]
    assert second_init["method"] == "droid.initialize_session"
    assert "sessionId" not in second_init["params"]
    assert session.sdk_session_id == "droid-new"
    assert session.resume_session_id is None
    assert session.chat_mode == "normal"
    assert session.model == "gpt-5.4"
    assert _continuation_ids(session).isdisjoint(old_ids)
    assert processes[0].terminated is True


@pytest.mark.asyncio
async def test_all_six_backends_are_covered_by_this_module() -> None:
    covered = {
        "claude": hasattr(ChatSession, "clear_context"),
        "codex": hasattr(CodexManagedChatSession, "clear_context"),
        "acp": hasattr(ACPManagedChatSession, "clear_context"),
        "droid": hasattr(DroidManagedChatSession, "clear_context"),
        "grok": hasattr(GrokManagedChatSession, "clear_context"),
        "qwen": hasattr(QwenManagedChatSession, "clear_context"),
    }
    assert list(covered) == list(_BACKENDS)
    missing = [name for name, present in covered.items() if not present]
    assert missing == []
