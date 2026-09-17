"""Plan directives travel through web lifecycle rules and provider send boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk.types import HookContext, UserPromptSubmitHookInput

from gobby.adapters.acp_client import ACPClient, StreamEvent
from gobby.hooks.events import ContextPart, HookEventType
from gobby.servers.chat_session import ChatSession
from gobby.servers.websocket.chat.backends.acp import ACPWebChatBackend
from gobby.servers.websocket.chat.backends.acp_session import ACPManagedChatSession
from gobby.servers.websocket.chat.backends.codex import (
    CodexManagedChatSession,
    CodexWebChatBackend,
)
from gobby.servers.websocket.chat.backends.droid import (
    DroidManagedChatSession,
    DroidWebChatBackend,
    _DroidProcessHandle,
)
from gobby.servers.websocket.chat.backends.grok import GrokManagedChatSession, GrokWebChatBackend
from gobby.servers.websocket.chat.backends.qwen import QwenManagedChatSession, QwenWebChatBackend
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from tests.servers.test_fire_lifecycle_parity import ChatMixinHost
from tests.workflows.test_plan_mode_rules import SESSION_ID, _create_session, _sync_bundled

pytestmark = pytest.mark.integration


class ProviderWire:
    """Controlled external transports; all Gobby send/lifecycle code stays real."""

    def __init__(self) -> None:
        self.fail = False
        self.prompts: list[str] = []
        self.handlers: dict[str, Any] = {}
        self.is_connected = True

    def capture(self, prompt: str) -> None:
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("provider rejected prompt")

    async def start_turn(self, thread_id: str, prompt: str, **kwargs: Any) -> SimpleNamespace:
        self.capture(str(kwargs.get("context_prefix") or "") + prompt)
        self.handlers["turn/completed"](
            "turn/completed", {"threadId": thread_id, "turnId": "turn", "usage": {}}
        )
        return SimpleNamespace(id="turn")

    def add_notification_handler(self, method: str, callback: Any) -> None:
        self.handlers[method] = callback

    def remove_notification_handler(self, method: str, callback: Any) -> None:
        del self.handlers[method]

    async def send(self, prompt: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.capture(json.dumps(prompt, ensure_ascii=False))
        yield StreamEvent(event_type="result", data={})

    def write(self, data: bytes) -> None:
        payload = json.loads(data)
        if payload["method"] == "droid.add_user_message":
            self.capture(payload["params"]["text"])

    async def drain(self) -> None:
        return None


def managed_session(provider: str, wire: ProviderWire, monkeypatch: pytest.MonkeyPatch) -> Any:
    backend: CodexWebChatBackend | DroidWebChatBackend | ACPWebChatBackend
    session: CodexManagedChatSession | DroidManagedChatSession | ACPManagedChatSession
    if provider == "codex":
        backend = CodexWebChatBackend(transcript_retry_attempts=0)
        session = CodexManagedChatSession(conversation_id="web-plan", _backend=backend)
        session._thread_id = "thread"
        monkeypatch.setattr(backend, "_client_for", lambda _: wire)
        monkeypatch.setattr(session, "_get_transcript_offset", AsyncMock(return_value=0))
        monkeypatch.setattr(session, "_get_transcript_records_since", AsyncMock(return_value=[]))
        monkeypatch.setattr(
            session, "_get_transcript_assistant_text_since", AsyncMock(return_value="")
        )
    elif provider == "droid":
        backend = DroidWebChatBackend()
        session = DroidManagedChatSession(conversation_id="web-plan", _backend=backend)
        backend._handles[session.conversation_id] = cast(
            _DroidProcessHandle,
            SimpleNamespace(
                process=SimpleNamespace(returncode=None, stdin=wire, stdout=object()),
                request_counter=0,
            ),
        )

        async def read_result(*args: Any) -> AsyncIterator[StreamEvent]:
            yield StreamEvent(event_type="result", data={})

        monkeypatch.setattr(backend, "_read_until_terminal", read_result)
    else:
        session_type, backend_type = {
            "qwen": (QwenManagedChatSession, QwenWebChatBackend),
            "grok": (GrokManagedChatSession, GrokWebChatBackend),
        }[provider]
        backend = backend_type()
        session = session_type(conversation_id="web-plan", _backend=backend)
        session._acp_client = cast(ACPClient, wire)
    backend._health.available = True
    session._connected = True
    session.sdk_session_id = "provider-session"
    session._model = "test-model"
    session._context_window_overrides = {"test-model": 100000}
    return session


def bind_rules(
    db: HubDatabase, session: Any, tmp_path: Path, request: pytest.FixtureRequest
) -> tuple[ChatMixinHost, SessionVariableManager]:
    _sync_bundled(db)
    _create_session(db)
    manager = SessionManager(db)
    variables = SessionVariableManager(db)
    variables.merge_variables(SESSION_ID, {"_agent_type": "default"})
    session.db_session_id = SESSION_ID
    session.project_path = str(tmp_path)
    session._on_mode_persist = lambda mode: manager.update_chat_mode(SESSION_ID, mode)
    host = ChatMixinHost()
    host._chat_sessions[session.conversation_id] = session
    host.workflow_handler = WorkflowHookHandler(
        rule_engine=RuleEngine(db),
        session_manager=manager,
        evaluation_runtime=WorkflowEvaluationRuntime(),
    )
    request.addfinalizer(host.workflow_handler.shutdown)
    session._on_before_agent = lambda data: host._fire_lifecycle(
        session.conversation_id, HookEventType.BEFORE_AGENT, data
    )
    return host, variables


@pytest.mark.parametrize("provider", ["codex", "qwen", "grok", "droid"])
async def test_managed_web_plan_delivery_periods(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    request: pytest.FixtureRequest,
) -> None:
    wire = ProviderWire()
    session = managed_session(provider, wire, monkeypatch)
    host, variables = bind_rules(temp_db, session, tmp_path, request)

    async def submit() -> str:
        _ = [event async for event in session.send_message("Investigate this change")]
        return wire.prompts[-1]

    def contains_directive(prompt: str) -> bool:
        # ACP serializes text blocks; the other transports carry a plain text field.
        directive = skill_fetch_directive("gobby:references/plan/overview.md")
        return directive in prompt or json.dumps(directive, ensure_ascii=False)[1:-1] in prompt

    session.set_chat_mode("normal")
    assert not contains_directive(await submit())
    session.set_chat_mode("plan")
    wire.fail = True
    if provider == "codex":
        events = [event async for event in session.send_message("Investigate this change")]
        assert any("provider rejected prompt" in str(event) for event in events)
    else:
        with pytest.raises(RuntimeError, match="provider rejected prompt"):
            await submit()
    assert contains_directive(wire.prompts[-1])
    assert variables.get_variables(SESSION_ID).get("plan_skill_directive_delivered") is not True
    wire.fail = False
    assert contains_directive(await submit())
    assert variables.get_variables(SESSION_ID)["plan_skill_directive_delivered"] is True
    assert not contains_directive(await submit())

    # Browser mode changes persist immediately, even with no intervening prompt.
    session.set_chat_mode("normal")
    session.set_chat_mode("plan")
    assert contains_directive(await submit())
    await host._fire_lifecycle(
        session.conversation_id, HookEventType.SESSION_START, {"source": "clear"}
    )
    assert contains_directive(await submit())


async def test_claude_web_directive_ack_requires_serialized_hook_output(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    session = ChatSession(conversation_id="web-claude")
    _, variables = bind_rules(temp_db, session, tmp_path, request)
    session.set_chat_mode("plan")
    hooks = session._build_sdk_hooks()
    assert hooks is not None
    prompt_hook = hooks["UserPromptSubmit"][0].hooks[0]
    context = HookContext(signal=None)
    prompt_input = UserPromptSubmitHookInput(
        hook_event_name="UserPromptSubmit",
        session_id=SESSION_ID,
        cwd=str(tmp_path),
        transcript_path="",
        prompt="Investigate",
    )
    with monkeypatch.context() as patcher:
        patcher.setattr(
            "gobby.servers.chat_session_hooks._response_to_prompt_output",
            MagicMock(side_effect=RuntimeError("serialization failed")),
        )
        with pytest.raises(RuntimeError, match="serialization failed"):
            await prompt_hook(prompt_input, None, context)
    assert variables.get_variables(SESSION_ID).get("plan_skill_directive_delivered") is not True
    output = await prompt_hook(prompt_input, None, context)
    serialized = json.loads(json.dumps(output))
    assert (
        skill_fetch_directive("gobby:references/plan/overview.md")
        in serialized["hookSpecificOutput"]["additionalContext"]
    )
    assert variables.get_variables(SESSION_ID)["plan_skill_directive_delivered"] is True
    output = await prompt_hook(prompt_input, None, context)
    serialized = json.loads(json.dumps(output))
    assert (
        skill_fetch_directive("gobby:references/plan/overview.md")
        not in serialized["hookSpecificOutput"]["additionalContext"]
    )


@pytest.mark.parametrize("provider", ["codex", "qwen", "grok", "droid"])
async def test_managed_blocked_prompt_never_reaches_provider(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    wire = ProviderWire()
    session = managed_session(provider, wire, monkeypatch)
    session._on_before_agent = AsyncMock(return_value={"decision": "block", "reason": "Wait"})
    with pytest.raises(RuntimeError, match="Wait"):
        _ = [event async for event in session.send_message("Blocked prompt")]
    assert wire.prompts == []


@pytest.mark.parametrize("provider", ["claude", "codex", "qwen", "grok", "droid"])
@pytest.mark.parametrize("reset", ["mode_change", "clear", "compact"])
async def test_context_change_during_evaluation_invalidates_older_receipt(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    reset: str,
    request: pytest.FixtureRequest,
) -> None:
    wire = ProviderWire()
    session = (
        ChatSession(conversation_id="web-claude")
        if provider == "claude"
        else managed_session(provider, wire, monkeypatch)
    )
    host, variables = bind_rules(temp_db, session, tmp_path, request)
    session.set_chat_mode("plan")
    evaluated = asyncio.Event()
    resume = asyncio.Event()

    async def pause_after_rules(event_type: HookEventType, *args: Any) -> list[ContextPart]:
        if event_type is HookEventType.BEFORE_AGENT:
            evaluated.set()
            await resume.wait()
        return []

    monkeypatch.setattr(host, "_dispatch_event_handlers", pause_after_rules)

    async def submit() -> None:
        if provider == "claude":
            hooks = session._build_sdk_hooks()
            assert hooks is not None
            await hooks["UserPromptSubmit"][0].hooks[0](
                UserPromptSubmitHookInput(
                    hook_event_name="UserPromptSubmit",
                    session_id=SESSION_ID,
                    cwd=str(tmp_path),
                    transcript_path="",
                    prompt="Investigate",
                ),
                None,
                HookContext(signal=None),
            )
        else:
            _ = [event async for event in session.send_message("Investigate")]

    async with asyncio.timeout(10):
        async with asyncio.TaskGroup() as group:
            group.create_task(submit())
            await evaluated.wait()
            if reset == "mode_change":
                session.set_chat_mode("normal")
                session.set_chat_mode("plan")
            else:
                await host._fire_lifecycle(
                    session.conversation_id,
                    HookEventType.SESSION_START if reset == "clear" else HookEventType.PRE_COMPACT,
                    {"source": reset},
                )
            resume.set()
    assert variables.get_variables(SESSION_ID)["plan_skill_directive_delivered"] is False
    await submit()
    assert variables.get_variables(SESSION_ID)["plan_skill_directive_delivered"] is True
