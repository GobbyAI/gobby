"""Question buttons answer a real pending interaction without taking over terminal input."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.models import ChannelConfig, CommsMessage
from gobby.communications.telegram_questions import QUESTION_ACTION, TelegramQuestionController
from gobby.servers.pending_interactions import PendingInteractionManager
from gobby.servers.routes.mcp.hook_hold_open import _maybe_hold_open
from gobby.sessions.turn_lifecycle import TurnLifecycleReducer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.datetime import utc_now

pytestmark = pytest.mark.unit
SESSION_ID = "22222222-2222-4222-8222-222222222221"
PROJECT_ID = "11111111-1111-4111-8111-111111111111"
TOOL_INPUT: dict[str, Any] = {
    "questions": [
        {
            "question": "Which framework?",
            "header": "Framework",
            "options": [
                {"label": "React", "description": "Component library"},
                {"label": "Vue", "description": "Progressive framework"},
            ],
            "multiSelect": False,
        }
    ]
}


@dataclass
class QuestionHarness:
    controller: TelegramQuestionController
    pending: PendingInteractionManager
    comms: MagicMock
    sent: asyncio.Event
    messages: list[CommsMessage]

    async def start(self, tool_input: dict[str, Any] | None = None) -> asyncio.Task[Any]:
        task = asyncio.create_task(
            self.controller.ask(self.pending, SESSION_ID, "", tool_input or TOOL_INPUT)
        )
        await self.wait_for_send(task)
        return task

    async def wait_for_send(self, task: asyncio.Task[Any]) -> None:
        # Readiness is event-driven; host load must not impose a first-send deadline.
        sent = asyncio.create_task(self.sent.wait())
        try:
            done, _ = await asyncio.wait({task, sent}, return_when=asyncio.FIRST_COMPLETED)
            if sent not in done:
                await task  # Surface delivery errors instead of hanging on the event.
                raise AssertionError("Question ended before sending its prompt")
        finally:
            sent.cancel()
            await asyncio.gather(sent, return_exceptions=True)

    def callback(self, option: object, *, question: int = 0) -> CommsMessage:
        return CommsMessage(
            id="answer",
            channel_id="telegram",
            direction="inbound",
            content="answer",
            created_at=utc_now(),
            session_id=SESSION_ID,
            content_type="callback",
            metadata_json={
                "callback_status": "ok",
                "callback_action": QUESTION_ACTION,
                "callback_value": json.dumps(
                    [self.messages[0].metadata_json["pending_question_id"], question, option]
                ),
                "chat_id": "42",
            },
        )


@pytest.fixture
async def harness(hub_db: HubDatabase) -> AsyncIterator[QuestionHarness]:
    with hub_db.transaction() as conn:
        conn.execute("INSERT INTO projects (id, name) VALUES (%s, 'test')", (PROJECT_ID,))
        conn.execute(
            """INSERT INTO sessions (id, external_id, machine_id, source, project_id, session_type)
               VALUES (%s, %s, '21000000-0000-4000-8000-000000000002', 'claude', %s, 'terminal')""",
            (SESSION_ID, SESSION_ID, PROJECT_ID),
        )
    pending = PendingInteractionManager(hub_db)
    comms = MagicMock()
    channel = ChannelConfig("telegram", "telegram", "telegram", True, {}, utc_now(), utc_now())
    comms.list_channels.return_value = [channel]
    comms.attached_destination.return_value = "dm:42"
    comms.attached_session.return_value = SESSION_ID
    comms.get_channel_by_name.return_value = channel
    comms.store.get_message_by_platform_id.return_value = None
    sent = asyncio.Event()
    messages: list[CommsMessage] = []

    async def send(
        channel_name: str, content: str, session_id: str, metadata: dict[str, Any]
    ) -> CommsMessage:
        message = CommsMessage(
            id=str(len(messages)),
            channel_id=channel_name,
            direction="outbound",
            content=content,
            created_at=utc_now(),
            session_id=session_id,
            metadata_json=metadata,
        )
        messages.append(message)
        sent.set()
        return message

    comms.send_message = AsyncMock(side_effect=send)
    yield QuestionHarness(TelegramQuestionController(comms), pending, comms, sent, messages)
    await pending.expire_all_pending()


@pytest.mark.asyncio
async def test_attached_question_sends_options_and_callback_answers(
    harness: QuestionHarness,
    hub_db: HubDatabase,
) -> None:
    task = await harness.start()
    assert len(harness.messages) == 1
    prompt = harness.messages[0]
    assert prompt.metadata_json["platform_destination"] == "42"
    assert "Which framework?" in prompt.content
    assert "React: Component library" in prompt.content
    assert "Vue: Progressive framework" in prompt.content
    assert [button["text"] for button in prompt.metadata_json["inline_keyboard"][0]] == [
        "React",
        "Vue",
        "Other",
    ]
    assert await harness.controller.handle("telegram", harness.callback(0))
    answer = await task
    decision = answer["hookSpecificOutput"]["decision"]
    assert decision == {
        "behavior": "allow",
        "updatedInput": {**TOOL_INPUT, "answers": {"Which framework?": "React"}},
    }
    assert await harness.controller.handle("telegram", harness.callback(1))
    assert harness.messages[-1].content == "This question is already resolved."
    sent_count = len(harness.messages)
    with patch.object(harness.pending, "resolve", wraps=harness.pending.resolve) as resolve:
        await harness.controller.terminal_answer(SESSION_ID, "native-tool-id", TOOL_INPUT)
        resolve.assert_not_awaited()
    assert not harness.controller._prompts
    assert len(harness.messages) == sent_count
    with hub_db.transaction() as conn:
        row = conn.execute(
            "SELECT status, response_json FROM pending_interactions WHERE id = %s",
            (prompt.metadata_json["pending_question_id"],),
        ).fetchone()
    assert row is not None
    assert row["status"] == "resolved"
    assert json.loads(row["response_json"])["source"] == "telegram"


@pytest.mark.asyncio
async def test_terminal_answer_wins_and_second_tap_is_resolved(harness: QuestionHarness) -> None:
    task = await harness.start()
    await harness.controller.terminal_answer(SESSION_ID, "native-tool-id", TOOL_INPUT)
    assert await task is None
    assert await harness.controller.handle("telegram", harness.callback(0))
    assert harness.messages[-1].content == "This question is already resolved."


@pytest.mark.asyncio
async def test_unattached_question_sends_nothing(harness: QuestionHarness) -> None:
    harness.comms.attached_destination.return_value = None
    assert await harness.controller.ask(harness.pending, SESSION_ID, "", TOOL_INPUT) is None
    assert harness.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("question_count", [1, 2])
async def test_reply_requires_other_tap(harness: QuestionHarness, question_count: int) -> None:
    tool_input = copy.deepcopy(TOOL_INPUT)
    if question_count == 2:
        tool_input["questions"].append(
            {**tool_input["questions"][0], "question": "Which language?"}
        )
    task = await harness.start(tool_input)
    harness.comms.store.get_message_by_platform_id.return_value = harness.messages[0]
    typed = CommsMessage(
        id="text",
        channel_id="telegram",
        direction="inbound",
        content="Svelte",
        created_at=utc_now(),
        session_id=SESSION_ID,
        metadata_json={"chat_id": "42", "reply_to_message_id": "prompt"},
    )
    assert await harness.controller.handle("telegram", typed)
    assert harness.messages[-1].content == "Choose Other on the question to answer."
    assert not task.done()
    await harness.controller.terminal_answer(SESSION_ID, "native-tool-id", tool_input)
    assert await task is None


@pytest.mark.asyncio
async def test_other_typed_answer_is_consumed(harness: QuestionHarness) -> None:
    task = await harness.start()
    assert await harness.controller.handle("telegram", harness.callback("other"))
    typed = CommsMessage(
        id="text",
        channel_id="telegram",
        direction="inbound",
        content="Svelte",
        created_at=utc_now(),
        session_id=SESSION_ID,
        metadata_json={"chat_id": "42"},
    )
    assert await harness.controller.handle("telegram", typed)
    answer = await task
    assert answer["hookSpecificOutput"]["decision"]["updatedInput"]["answers"] == {
        "Which framework?": "Svelte"
    }


@pytest.mark.asyncio
async def test_wrong_session_cannot_answer(harness: QuestionHarness) -> None:
    task = await harness.start()
    callback = harness.callback(0)
    callback.session_id = "another-session"
    assert await harness.controller.handle("telegram", callback)
    assert not task.done()
    await harness.controller.terminal_answer(SESSION_ID, "native-tool-id", TOOL_INPUT)
    assert await task is None


@pytest.mark.asyncio
async def test_multi_select_requires_done(harness: QuestionHarness) -> None:
    question = {**TOOL_INPUT["questions"][0], "multiSelect": True}
    task = await harness.start({"questions": [question]})
    assert await harness.controller.handle("telegram", harness.callback(0))
    assert await harness.controller.handle("telegram", harness.callback(1))
    assert not task.done()
    assert await harness.controller.handle("telegram", harness.callback("done"))
    answer = await task
    assert answer["hookSpecificOutput"]["decision"]["updatedInput"]["answers"] == {
        "Which framework?": "React, Vue"
    }


@pytest.mark.asyncio
async def test_native_permission_hook_and_terminal_completion(
    harness: QuestionHarness, hub_db: HubDatabase
) -> None:
    request = MagicMock()
    server = request.app.state.server
    server.services.database = hub_db
    server.services.communications_manager = harness.comms
    harness.comms.telegram_questions = harness.controller
    request.app.state.pending_interaction_manager = harness.pending

    async def run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(func, *args, **kwargs)

    server.run_db = AsyncMock(side_effect=run_db)
    payload = {"input_data": {"tool_name": "AskUserQuestion", "tool_input": TOOL_INPUT}}
    task = asyncio.create_task(
        _maybe_hold_open(request, SESSION_ID, "PermissionRequest", payload, "claude")
    )
    await harness.wait_for_send(task)
    assert len(harness.messages) == 1
    completed = {
        "input_data": {
            "tool_name": "AskUserQuestion",
            "tool_use_id": "native-id",
            "tool_input": TOOL_INPUT,
            "tool_response": {"answers": {"Which framework?": "Vue"}},
        }
    }
    assert await _maybe_hold_open(request, SESSION_ID, "PostToolUse", completed, "claude") is None
    assert await task is None
    assert await harness.controller.handle("telegram", harness.callback(0))
    assert harness.messages[-1].content == "This question is already resolved."


@pytest.mark.asyncio
async def test_hook_cancellation_expires_question(
    harness: QuestionHarness, hub_db: HubDatabase
) -> None:
    task = await harness.start()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = hub_db.fetchone(
        "SELECT status FROM pending_interactions WHERE id = %s",
        (harness.messages[0].metadata_json["pending_question_id"],),
    )
    assert row is not None and row["status"] == "expired"
    assert await harness.controller.handle("telegram", harness.callback(0))
    assert harness.messages[-1].content == "This question is already resolved."


@pytest.mark.asyncio
async def test_delivery_failure_expires_question(
    harness: QuestionHarness, hub_db: HubDatabase
) -> None:
    harness.comms.send_message.side_effect = RuntimeError("Telegram unavailable")
    with pytest.raises(RuntimeError, match="Telegram unavailable"):
        await harness.controller.ask(harness.pending, SESSION_ID, "", TOOL_INPUT)
    row = hub_db.fetchone(
        "SELECT status FROM pending_interactions WHERE session_id = %s", (SESSION_ID,)
    )
    assert row is not None and row["status"] == "expired"


@pytest.mark.asyncio
async def test_partial_selection_refreshes_buttons_and_resolution_retires_them(
    harness: QuestionHarness,
) -> None:
    harness.comms._adapters.get.return_value = MagicMock(spec=TelegramAdapter)
    with patch(
        "gobby.communications.telegram_questions.edit_keyboard_message", new_callable=AsyncMock
    ) as edit:
        question = {**TOOL_INPUT["questions"][0], "multiSelect": True}
        task = await harness.start({"questions": [question]})
        assert await harness.controller.handle("telegram", harness.callback(0))
        assert edit.await_args is not None
        assert edit.await_args.args[-1][0][0]["text"] == "✓ React"
        assert await harness.controller.handle("telegram", harness.callback(0))
        assert edit.await_args.args[-1][0][0]["text"] == "React"
        await harness.controller.terminal_answer(SESSION_ID, "native-id", {"questions": [question]})
        assert await task is None
        assert edit.await_args.args[-1] is None
        assert "resolved" in edit.await_args.args[3]


@pytest.mark.asyncio
async def test_all_questions_must_be_answered(harness: QuestionHarness) -> None:
    second = {**TOOL_INPUT["questions"][0], "question": "Which second framework?"}
    task = await harness.start({"questions": [TOOL_INPUT["questions"][0], second]})
    assert await harness.controller.handle("telegram", harness.callback(0))
    assert not task.done()
    assert await harness.controller.handle("telegram", harness.callback(1, question=1))
    answer = await task
    assert answer["hookSpecificOutput"]["decision"]["updatedInput"]["answers"] == {
        "Which framework?": "React",
        "Which second framework?": "Vue",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["telegram", "terminal"])
async def test_answer_clears_lifecycle_wait(
    harness: QuestionHarness, hub_db: HubDatabase, source: str
) -> None:
    lifecycle = TurnLifecycleReducer(SessionManager(hub_db))
    task = await harness.start()
    assert len(lifecycle.get(SESSION_ID).waits) == 1
    if source == "telegram":
        assert await harness.controller.handle("telegram", harness.callback(0))
    else:
        await harness.controller.terminal_answer(SESSION_ID, "native-tool-id", TOOL_INPUT)
    await task
    assert lifecycle.get(SESSION_ID).waits == ()


@pytest.mark.asyncio
async def test_button_after_other_leaves_unrelated_text_for_session(
    harness: QuestionHarness,
) -> None:
    second = {**TOOL_INPUT["questions"][0], "question": "Which second framework?"}
    task = await harness.start({"questions": [TOOL_INPUT["questions"][0], second]})
    assert await harness.controller.handle("telegram", harness.callback("other"))
    assert await harness.controller.handle("telegram", harness.callback(0))
    text = CommsMessage(
        id="unrelated",
        channel_id="telegram",
        direction="inbound",
        content="New message",
        created_at=utc_now(),
        session_id=SESSION_ID,
        metadata_json={"chat_id": "42"},
    )
    assert not await harness.controller.handle("telegram", text)
    assert await harness.controller.handle("telegram", harness.callback(1, question=1))
    answer = await task
    assert answer["hookSpecificOutput"]["decision"]["updatedInput"]["answers"] == {
        "Which framework?": "React",
        "Which second framework?": "Vue",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_input",
    [
        {},
        {"questions": "invalid"},
        {"questions": [{"question": "Which?", "options": []}]},
        {"questions": [TOOL_INPUT["questions"][0], TOOL_INPUT["questions"][0]]},
    ],
)
async def test_invalid_prompt_leaves_terminal_path_live(
    harness: QuestionHarness, tool_input: dict[str, Any]
) -> None:
    assert await harness.controller.ask(harness.pending, SESSION_ID, "", tool_input) is None
    assert harness.messages == []


@pytest.mark.asyncio
async def test_feedback_failure_does_not_fan_answer_out(harness: QuestionHarness) -> None:
    task = await harness.start()
    harness.comms.send_message.side_effect = RuntimeError("Acknowledgement unavailable")
    assert await harness.controller.handle("telegram", harness.callback(0))
    answer = await task
    assert answer["hookSpecificOutput"]["decision"]["updatedInput"]["answers"] == {
        "Which framework?": "React"
    }


@pytest.mark.asyncio
async def test_reaction_is_not_an_other_answer(harness: QuestionHarness) -> None:
    task = await harness.start()
    assert await harness.controller.handle("telegram", harness.callback("other"))
    reaction = CommsMessage(
        id="reaction",
        channel_id="telegram",
        direction="inbound",
        content="like",
        created_at=utc_now(),
        session_id=SESSION_ID,
        content_type="reaction",
        metadata_json={"chat_id": "42"},
    )
    assert not await harness.controller.handle("telegram", reaction)
    assert not task.done()
    await harness.controller.terminal_answer(SESSION_ID, "native-id", TOOL_INPUT)
    assert await task is None
