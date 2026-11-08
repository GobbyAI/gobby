"""Context paging across deliveries: queued parts ship first and in order, nothing is dropped.

Each delivery runs inside its staging scope and acknowledges its receipt, as the hook
pipeline does, so the queue persists in the hub database between deliveries.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from claude_agent_sdk.types import PreToolUseHookSpecificOutput, SyncHookJSONOutput

from gobby.adapters.capabilities import ContextChannel
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.degradation import (
    CARRIED_CONTEXT_VARIABLE,
    ContextQueueUnavailableError,
    page_context_for_adapter,
)
from gobby.adapters.droid import DroidAdapter
from gobby.hooks.events import HookResponse, SessionSource
from gobby.hooks.receipt_effects import (
    apply_acknowledged_receipt,
    take_worker_staging,
    worker_staging_scope,
)
from gobby.llm.sdk_utils import ADDITIONAL_CONTEXT_LIMIT
from gobby.servers.chat_session_helpers import (
    _bound_resp_context,
    _response_to_post_tool_output,
    _response_to_stop_output,
)
from gobby.servers.chat_session_hooks import ChatSessionHooksMixin
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

_CHANNEL_VARIABLE = f"{CARRIED_CONTEXT_VARIABLE}_{ContextChannel.SYSTEM_MESSAGE}"
_BREADCRUMB = re.compile(r"\n\ncontext continues on the next delivery: [^\n]*\Z")
_PAGE_HEADER = re.compile(r"\[[^\]\n]* \(page \d+/\d+\)\]\n")
_LEADING_PAGE_NUMBER = re.compile(r"\A\[[^\]\n]* \(page (\d+)/\d+\)\]\n")


def _reassemble_serialized(texts: list[str]) -> str:
    """Rebuild the original bytes from each delivery's serialized text, not its contributors.

    A delivery's text loses its breadcrumb and page headers and nothing else. Inside a
    delivery the text already carries its blank lines. Between deliveries a whole part starts
    on a blank line, and a continuation page joins the page before it directly.
    """
    pieces: list[str] = []
    for text in texts:
        body = _BREADCRUMB.sub("", text)
        leading = _LEADING_PAGE_NUMBER.match(body)
        continues = leading is not None and int(leading.group(1)) > 1
        if pieces and not continues:
            pieces.append("\n\n")
        pieces.append(_PAGE_HEADER.sub("", body))
    return "".join(pieces)


def _deliver(db: HubDatabase, translate: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Translate one delivery inside its staging scope and acknowledge its receipt."""
    with worker_staging_scope():
        result = translate()
        staged = take_worker_staging()
    apply_acknowledged_receipt(
        SimpleNamespace(staged_payload=staged),
        variable_manager=SessionVariableManager(db),
    )
    return result


def _register_session(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, machine: str) -> str:
    monkeypatch.setattr("gobby.storage.workspace_machine_scope.require_machine_id", lambda: machine)
    LocalMachineManager(temp_db).upsert_seen(machine, TEST_USER_ID)
    return (
        SessionManager(temp_db)
        .register(external_id=str(uuid4()), machine_id=machine, source="claude", project_id=None)
        .id
    )


def _queue(temp_db: HubDatabase, session_id: str, variable: str = CARRIED_CONTEXT_VARIABLE) -> Any:
    return SessionVariableManager(temp_db).get_variables(session_id).get(variable)


def _claude_context(
    adapter: ClaudeCodeAdapter, temp_db: HubDatabase, response: HookResponse
) -> str:
    result = _deliver(
        temp_db,
        lambda: adapter.translate_from_hook_response(response, hook_type="user-prompt-submit"),
    )
    return str(result["hookSpecificOutput"]["additionalContext"])


@pytest.mark.parametrize(
    ("adapter_type", "hook_type"),
    [(ClaudeCodeAdapter, "user-prompt-submit"), (DroidAdapter, "UserPromptSubmit")],
    ids=["claude-code", "droid"],
)
def test_an_empty_delivery_drains_the_queue(
    adapter_type: type[ClaudeCodeAdapter] | type[DroidAdapter],
    hook_type: str,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued remainder is owed to the next delivery, even one with nothing fresh to say."""
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-000000000002")
    adapter = adapter_type(hook_manager=cast(Any, SimpleNamespace(db=temp_db)))
    response = HookResponse(decision="allow", metadata={"session_id": session_id})
    body = "\n\n".join("b" * 99 for _ in range(104))
    assert len(body) > ADDITIONAL_CONTEXT_LIMIT
    response.add_context(("rule:big", body))
    first = _deliver(
        temp_db,
        lambda: adapter.translate_from_hook_response(response, hook_type=hook_type),
    )
    assert _queue(temp_db, session_id)

    # Enrichment stamps the session on every hook; a later hook has no fresh contributors.
    empty_response = HookResponse(decision="allow", metadata={"session_id": session_id})
    empty = _deliver(
        temp_db,
        lambda: adapter.translate_from_hook_response(empty_response, hook_type=hook_type),
    )

    first_context = str(first.get("hookSpecificOutput", {}).get("additionalContext", ""))
    carried = str(empty.get("hookSpecificOutput", {}).get("additionalContext", ""))
    assert carried.startswith("[rule:big (page 2/2)]\n")
    assert "context continues" not in carried
    assert _reassemble_serialized([first_context, carried]) == body
    assert not _queue(temp_db, session_id)


def test_parts_that_do_not_fit_without_a_session_queue_raise_and_ship_nothing() -> None:
    """With no queue to wait in, the parts that do not fit fail loudly; none is dropped."""
    parts = [("rule:a", "a" * 9_000), ("rule:b", "b" * 2_000)]

    with pytest.raises(ContextQueueUnavailableError) as raised:
        page_context_for_adapter(
            parts,
            provider=SessionSource.CLAUDE,
            hook_type=None,
            destination_channel=ContextChannel.ADDITIONAL_CONTEXT,
        )

    assert raised.value.labels == ["rule:b"]


def test_parts_that_fit_without_a_session_queue_ship_whole() -> None:
    text = page_context_for_adapter(
        [("rule:a", "a" * 40), ("rule:b", "b" * 60)],
        provider=SessionSource.CLAUDE,
        hook_type=None,
        destination_channel=ContextChannel.ADDITIONAL_CONTEXT,
    )

    assert text == "a" * 40 + "\n\n" + "b" * 60


def test_three_times_the_cap_arrives_over_consecutive_deliveries_in_order(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-000000000001")
    adapter = ClaudeCodeAdapter(hook_manager=cast(Any, SimpleNamespace(db=temp_db)))
    big = "\n\n".join(f"section {index} " + "s" * 780 for index in range(24))
    parts = [
        ("rule:policy", "o" * 4_000),
        ("rule:big", big),
        ("rule:common", "c" * 4_000),
        ("rule:tail", "t" * 4_000),
    ]
    assert len(big) > ADDITIONAL_CONTEXT_LIMIT
    assert sum(len(text) for _, text in parts) > 3 * ADDITIONAL_CONTEXT_LIMIT

    response = HookResponse(decision="allow", metadata={"session_id": session_id})
    response.add_context(*parts)
    deliveries = [_claude_context(adapter, temp_db, response)]
    while _queue(temp_db, session_id):
        # A later hook with nothing fresh still drains the queue, one page per delivery.
        later = HookResponse(decision="allow", metadata={"session_id": session_id})
        deliveries.append(_claude_context(adapter, temp_db, later))
        assert len(deliveries) < 20, "the queue must drain"

    assert len(deliveries) >= 3
    for text in deliveries:
        assert len(text) <= ADDITIONAL_CONTEXT_LIMIT
    assert "context continues" not in deliveries[-1]
    assert _reassemble_serialized(deliveries) == "\n\n".join(body for _, body in parts)


def test_queued_parts_ship_before_fresh_parts_and_nothing_jumps_ahead(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-000000000002")
    adapter = ClaudeCodeAdapter(hook_manager=cast(Any, SimpleNamespace(db=temp_db)))

    first = HookResponse(decision="allow", metadata={"session_id": session_id})
    first.add_context(("rule:policy", "o" * 4_000), ("rule:big", "b" * 9_000))
    first.add_context(("agent_prompt", "p" * 6_000), prepend=True)
    assert _claude_context(adapter, temp_db, first) == (
        "p" * 6_000 + "\n\ncontext continues on the next delivery: rule:policy, rule:big"
    )

    second = HookResponse(decision="allow", metadata={"session_id": session_id})
    second.add_context(("rule:reminder", "be brief"))
    assert _claude_context(adapter, temp_db, second) == (
        "o" * 4_000 + "\n\ncontext continues on the next delivery: rule:big, rule:reminder"
    )
    assert _queue(temp_db, session_id) == [
        ["rule:big", "b" * 9_000],
        ["rule:reminder", "be brief"],
    ]


def test_a_queued_part_reinjected_every_turn_is_queued_once(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-000000000003")
    adapter = ClaudeCodeAdapter(hook_manager=cast(Any, SimpleNamespace(db=temp_db)))

    first = HookResponse(decision="allow", metadata={"session_id": session_id})
    first.add_context(("rule:policy", "o" * 6_000), ("rule:common", "c" * 5_000))
    first.add_context(("agent_prompt", "p" * 6_000), prepend=True)
    _claude_context(adapter, temp_db, first)
    assert _queue(temp_db, session_id) == [
        ["rule:policy", "o" * 6_000],
        ["rule:common", "c" * 5_000],
    ]

    # The workflow re-injects both parts every turn; the queued copies stand in for them.
    second = HookResponse(decision="allow", metadata={"session_id": session_id})
    second.add_context(("rule:policy", "o" * 6_000), ("rule:common", "c" * 5_000))
    assert _claude_context(adapter, temp_db, second) == (
        "o" * 6_000 + "\n\ncontext continues on the next delivery: rule:common"
    )
    assert _queue(temp_db, session_id) == [["rule:common", "c" * 5_000]]

    third = HookResponse(decision="allow", metadata={"session_id": session_id})
    third.add_context(("rule:common", "c" * 5_000))
    assert _claude_context(adapter, temp_db, third) == "c" * 5_000
    assert _queue(temp_db, session_id) is None


def test_codex_deny_system_message_carries_its_queue_to_the_next_block(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-000000000004")
    adapter = CodexHooksAdapter(hook_manager=cast(Any, SimpleNamespace(db=temp_db)))

    first = HookResponse(
        decision="block",
        reason="blocked by rule",
        system_message="Rule notice",
        metadata={"session_id": session_id},
    )
    first.add_context(("rule:a", "a" * 6_000), ("rule:b", "b" * 6_000))
    first_message = _deliver(
        temp_db,
        lambda: adapter.translate_from_hook_response(first, hook_type="PreToolUse"),
    )["systemMessage"]
    assert first_message == (
        "Rule notice\n\n" + "a" * 6_000 + "\n\ncontext continues on the next delivery: rule:b"
    )
    assert _queue(temp_db, session_id, _CHANNEL_VARIABLE) == [["rule:b", "b" * 6_000]]

    second = HookResponse(
        decision="block",
        reason="blocked by rule",
        metadata={"session_id": session_id},
    )
    second.add_context(("rule:c", "c" * 100))
    second_message = _deliver(
        temp_db,
        lambda: adapter.translate_from_hook_response(second, hook_type="PreToolUse"),
    )["systemMessage"]
    assert second_message == "b" * 6_000 + "\n\n" + "c" * 100
    assert _queue(temp_db, session_id, _CHANNEL_VARIABLE) is None


class _ChatHooks(ChatSessionHooksMixin):
    """The SDK hook surface of one chat session, with the lifecycle callback stubbed."""

    def __init__(self, temp_db: HubDatabase, session_id: str) -> None:
        self.conversation_id = "chat-paging"
        self.db_session_id = session_id
        self.project_id = None
        self.chat_mode = "default"
        self._needs_history_injection = False
        self._plan_approved = False
        self._plan_broadcast_sent = False
        self._preapproved_tool_use_ids = set()
        self._session_manager_ref = SimpleNamespace(db=temp_db)
        self._transcript_path_captured = True
        self._on_before_agent = None
        self._on_prompt_delivered = None
        self._on_pre_tool = None
        self._on_post_tool = None
        self._on_stop = None
        self._on_pre_compact = None
        self._on_subagent_start = None
        self._on_subagent_stop = None
        self._on_plan_ready = None
        self._pending_plan: str | None = None

    async def _load_history_context(self) -> str:
        return "<history>earlier turns</history>"

    def _consume_plan_mode_context(self) -> str | None:
        feedback, self._pending_plan = self._pending_plan, None
        return feedback

    def _needs_tool_approval(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        return False

    async def _resolve_tool_permission(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(updated_input=None)

    async def pre_tool(self, parts: list[tuple[str, str]]) -> dict[str, Any]:
        """Run one allowed PreToolUse through the SDK hook the chat session registers."""
        resp = {
            "decision": "allow",
            "context": "\n\n".join(text for _, text in parts),
            "context_parts": list(parts),
        }

        async def before_tool(data: dict[str, Any]) -> dict[str, Any]:
            return resp

        self._on_pre_tool = before_tool
        hooks = self._build_sdk_hooks()
        assert hooks is not None
        matcher = hooks["PreToolUse"][0]
        output = await matcher.hooks[0](
            cast(Any, {"tool_name": "Read", "tool_input": {}}), "tool-1", cast(Any, None)
        )
        return cast(dict[str, Any], output)

    async def prompt(self, decision: str, parts: list[tuple[str, str]]) -> dict[str, Any]:
        """Run one UserPromptSubmit through the SDK hook the chat session registers."""
        resp = {
            "decision": decision,
            "reason": "blocked by rule" if decision == "block" else None,
            "context": "\n\n".join(text for _, text in parts),
            "context_parts": list(parts),
        }

        async def before_agent(data: dict[str, Any]) -> dict[str, Any]:
            return resp

        self._on_before_agent = before_agent
        hooks = self._build_sdk_hooks()
        assert hooks is not None
        matcher = hooks["UserPromptSubmit"][0]
        output = await matcher.hooks[0](cast(Any, {"prompt": "hello"}), None, cast(Any, None))
        return cast(dict[str, Any], output)


async def test_web_chat_prompt_carries_its_queue_to_the_next_prompt(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-000000000005")
    chat = _ChatHooks(temp_db, session_id)

    first = await chat.prompt("allow", [("rule:policy", "o" * 6_000), ("rule:common", "c" * 5_000)])
    assert first["hookSpecificOutput"]["additionalContext"] == (
        "o" * 6_000 + "\n\ncontext continues on the next delivery: rule:common"
    )
    assert _queue(temp_db, session_id) == [["rule:common", "c" * 5_000]]

    second = await chat.prompt("allow", [("rule:reminder", "be brief")])
    assert second["hookSpecificOutput"]["additionalContext"] == "c" * 5_000 + "\n\nbe brief"
    assert _queue(temp_db, session_id) is None


async def test_an_empty_web_chat_turn_drains_the_queue(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-000000000007")
    chat = _ChatHooks(temp_db, session_id)

    await chat.prompt("allow", [("rule:policy", "o" * 6_000), ("rule:common", "c" * 5_000)])
    assert _queue(temp_db, session_id) == [["rule:common", "c" * 5_000]]

    # A turn with nothing fresh to say still carries the queued remainder.
    empty = await chat.prompt("allow", [])
    assert empty["hookSpecificOutput"]["additionalContext"] == "c" * 5_000
    assert _queue(temp_db, session_id) is None


async def test_web_chat_blocked_prompt_commits_no_queue(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-000000000006")
    chat = _ChatHooks(temp_db, session_id)

    blocked = await chat.prompt(
        "block", [("rule:policy", "o" * 6_000), ("rule:common", "c" * 5_000)]
    )

    assert blocked["decision"] == "block"
    assert "hookSpecificOutput" not in blocked
    assert _queue(temp_db, session_id) is None


async def test_a_blocked_prompt_leaves_one_shot_prompt_context_for_the_next_prompt(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """History and plan feedback are owed to a prompt that ships, not to a blocked one."""
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-000000000007")
    chat = _ChatHooks(temp_db, session_id)
    chat._needs_history_injection = True
    chat._pending_plan = "revise step 2"

    blocked = await chat.prompt("block", [])
    assert blocked["decision"] == "block"

    delivered = await chat.prompt("allow", [])
    context = delivered["hookSpecificOutput"]["additionalContext"]
    assert "<history>earlier turns</history>" in context
    assert "revise step 2" in context
    assert chat._needs_history_injection is False
    assert chat._pending_plan is None


async def test_a_tool_call_with_nothing_fresh_still_drains_the_queue(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-000000000008")
    chat = _ChatHooks(temp_db, session_id)

    await chat.prompt("allow", [("rule:policy", "o" * 9_000), ("rule:common", "c" * 3_000)])
    assert _queue(temp_db, session_id) == [["rule:common", "c" * 3_000]]

    output = await chat.pre_tool([])

    assert output["hookSpecificOutput"]["additionalContext"] == "c" * 3_000
    assert not _queue(temp_db, session_id)


async def test_a_tool_result_with_no_callback_result_still_drains_the_queue(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-00000000000a")
    chat = _ChatHooks(temp_db, session_id)
    await chat.prompt("allow", [("rule:policy", "o" * 9_000), ("rule:common", "c" * 3_000)])

    async def post_tool(inp: Any, tool_use_id: Any, ctx: Any) -> SyncHookJSONOutput:
        return _response_to_post_tool_output(None, persist=chat._additional_context_persist())

    output = await chat._staged_hook(post_tool)(cast(Any, {}), None, cast(Any, None))

    hook = cast(dict[str, Any], output["hookSpecificOutput"])
    assert hook["additionalContext"] == "c" * 3_000
    assert not _queue(temp_db, session_id)


def _receipt_states(temp_db: HubDatabase, session_id: str) -> list[str]:
    with temp_db.transaction() as conn:
        rows = conn.execute(
            "SELECT state FROM hook_receipt_effects WHERE session_id = %s ORDER BY created_at",
            (session_id,),
        ).fetchall()
    return [str(row["state"]) for row in rows]


def test_a_shipped_chat_output_commits_its_effects_through_an_acknowledged_receipt(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-00000000000b")
    chat = _ChatHooks(temp_db, session_id)

    chat._commit_staged_context({"session_variables": {"chat_probe": "shipped"}})

    assert _receipt_states(temp_db, session_id) == ["acknowledged"]
    assert SessionVariableManager(temp_db).get_variables(session_id)["chat_probe"] == "shipped"


def test_a_chat_commit_that_loses_the_receipt_cas_applies_no_effects(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-00000000000c")
    chat = _ChatHooks(temp_db, session_id)
    monkeypatch.setattr("gobby.storage.hook_receipts.acknowledge_receipt", lambda *_a, **_k: None)

    chat._commit_staged_context({"session_variables": {"chat_probe": "shipped"}})

    assert "chat_probe" not in SessionVariableManager(temp_db).get_variables(session_id)


async def test_a_blocked_stop_delivers_no_context_and_commits_no_queue(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-00000000000b")
    chat = _ChatHooks(temp_db, session_id)
    blocked = {
        "decision": "block",
        "reason": "unfinished work",
        "context_parts": [("rule:policy", "o" * 9_000), ("rule:common", "c" * 3_000)],
    }

    async def stop(inp: Any, tool_use_id: Any, ctx: Any) -> SyncHookJSONOutput:
        return _response_to_stop_output(blocked, persist=chat._additional_context_persist())

    output = await chat._staged_hook(stop)(cast(Any, {}), None, cast(Any, None))

    assert output["decision"] == "block"
    assert "hookSpecificOutput" not in output
    assert _queue(temp_db, session_id) is None


async def test_a_denied_tool_call_commits_no_staged_queue(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "21000000-0000-4000-8000-00000000000c")
    chat = _ChatHooks(temp_db, session_id)

    async def denied(inp: Any, tool_use_id: Any, ctx: Any) -> SyncHookJSONOutput:
        # Overflow stages a queue inside the hook's staging scope; the deny must not commit it.
        _bound_resp_context(
            {},
            [("rule:policy", "o" * 9_000), ("rule:common", "c" * 3_000)],
            persist=chat._additional_context_persist(),
        )
        output = SyncHookJSONOutput()
        output["hookSpecificOutput"] = PreToolUseHookSpecificOutput(
            hookEventName="PreToolUse", permissionDecision="deny"
        )
        return output

    output = await chat._staged_hook(denied)(cast(Any, {}), None, cast(Any, None))

    hook = cast(dict[str, Any], output["hookSpecificOutput"])
    assert hook["permissionDecision"] == "deny"
    assert _queue(temp_db, session_id) is None
