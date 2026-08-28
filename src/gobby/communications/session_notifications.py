"""Actionable lifecycle notifications backed by rendered session transcripts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

from gobby.communications.native_plan_actions import encode_native_plan_option
from gobby.communications.session_events import route_session_status_transition
from gobby.sessions.compact_markers import (
    COMPACT_NOTIFICATION_STARTED_AT_VARIABLE,
    HANDOFF_COMPACT_CONTINUE_FRESH_SECONDS,
)
from gobby.sessions.status_events import SessionStatusTransition
from gobby.sessions.transcript_render_models import RenderedMessage, RenderedToolCall
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.communications.manager import CommunicationsManager
    from gobby.communications.native_plan_actions import NativePlanActionService
    from gobby.sessions.transcript_reader import TranscriptReader
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

_COMPACTION_TOOL_NAMES = {"set_handoff", "get_handoff"}
_QUESTION_TOOL_NAME = "request_user_input"
_REPLY_AFFORDANCE = "Reply to any part of this message with custom instructions."


@dataclass(frozen=True)
class PausePresentation:
    """Transcript-derived content and actions for one lifecycle message."""

    assistant_message: str | None
    keyboard: list[list[dict[str, str]]] | None
    has_real_activity: bool
    is_question: bool


class Sleep(Protocol):
    async def __call__(self, delay: float) -> None: ...


class SessionNotificationService:
    """Render lifecycle transitions and defer compaction-only pauses."""

    def __init__(
        self,
        manager: CommunicationsManager,
        session_manager: SessionManager,
        transcript_reader: TranscriptReader,
        *,
        native_plan_actions: NativePlanActionService | None = None,
        sleep: Sleep = asyncio.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._manager = manager
        self._session_manager = session_manager
        self._transcript_reader = transcript_reader
        self._variables = SessionVariableManager(session_manager.db)
        self._native_plan_actions = native_plan_actions
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(UTC))
        self._pending: dict[str, tuple[datetime, asyncio.Task[None]]] = {}

    async def start(self) -> None:
        """Recover compact evaluations that survived a daemon restart."""
        rows = await asyncio.to_thread(
            self._session_manager.db.fetchall,
            """
            SELECT session_id, variables->>%s AS compact_started_at
              FROM session_variables
             WHERE NULLIF(variables->>%s, '') IS NOT NULL
            """,
            (
                COMPACT_NOTIFICATION_STARTED_AT_VARIABLE,
                COMPACT_NOTIFICATION_STARTED_AT_VARIABLE,
            ),
        )
        for row in rows:
            started_at = _parse_utc(row["compact_started_at"])
            if started_at is not None:
                self._schedule(str(row["session_id"]), started_at)

    async def stop(self) -> None:
        """Cancel outstanding deadline evaluations."""
        tasks = [task for _, task in self._pending.values()]
        self._pending.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def route_transition(self, transition: SessionStatusTransition) -> None:
        """Route a committed pause/expiry transition."""
        if transition.status not in {"paused", "expired"}:
            return

        compact_started_at = await asyncio.to_thread(
            self._compact_started_at,
            transition.session_id,
        )
        presentation = await self._presentation(
            transition.session_id,
            since=compact_started_at,
        )
        if (
            transition.status == "paused"
            and compact_started_at is not None
            and not presentation.has_real_activity
        ):
            self._schedule(transition.session_id, compact_started_at)
            return

        if compact_started_at is not None:
            await asyncio.to_thread(
                self._clear_compact_marker,
                transition.session_id,
                compact_started_at,
            )
        await self._send_transition(transition, presentation)

    async def _send_transition(
        self,
        transition: SessionStatusTransition,
        presentation: PausePresentation,
        *,
        label: str | None = None,
        event_id: str | None = None,
    ) -> None:
        metadata: dict[str, Any] | None = None
        assistant_message = presentation.assistant_message
        if transition.status == "paused":
            keyboard = presentation.keyboard if presentation.is_question else None
            native_menu = None
            if not presentation.is_question and self._native_plan_actions is not None:
                try:
                    native_menu = await self._native_plan_actions.get_menu(transition.session_id)
                except Exception:
                    logger.exception(
                        "Failed to read native plan menu for session %s",
                        transition.session_id,
                    )
            if native_menu is not None:
                keyboard = [
                    [
                        {
                            "text": choice.label,
                            "value": encode_native_plan_option(choice.option),
                        }
                    ]
                    for choice in native_menu.choices
                ]
            elif not presentation.is_question:
                keyboard = [[{"text": "Continue", "value": "Continue"}]]
            metadata = {
                "lifecycle_actionable": True,
                "actionable_session_id": transition.session_id,
                "lifecycle_project_id": transition.project_id,
                "callback_action": "session_action",
            }
            if keyboard is not None:
                metadata["inline_keyboard"] = keyboard
            if native_menu is not None:
                metadata["native_plan_fingerprint"] = native_menu.fingerprint
            assistant_message = _append_reply_affordance(assistant_message)
        await route_session_status_transition(
            self._manager,
            transition,
            label=label,
            assistant_message=assistant_message,
            event_id=event_id,
            metadata=metadata,
        )

    def _schedule(self, session_id: str, started_at: datetime) -> None:
        existing = self._pending.get(session_id)
        if existing is not None:
            existing_started_at, existing_task = existing
            if existing_started_at == started_at and not existing_task.done():
                return
            existing_task.cancel()
        task = asyncio.create_task(
            self._evaluate_at_deadline(session_id, started_at),
            name=f"compact-notification:{session_id}",
        )
        self._pending[session_id] = (started_at, task)
        task.add_done_callback(lambda completed: self._forget_pending(session_id, completed))

    def _forget_pending(
        self,
        session_id: str,
        completed: asyncio.Task[None],
    ) -> None:
        current = self._pending.get(session_id)
        if current is not None and current[1] is completed:
            self._pending.pop(session_id, None)

    async def _evaluate_at_deadline(self, session_id: str, started_at: datetime) -> None:
        deadline = started_at.timestamp() + HANDOFF_COMPACT_CONTINUE_FRESH_SECONDS
        delay = max(0.0, deadline - self._now().timestamp())
        if delay:
            await self._sleep(delay)

        current_started_at = await asyncio.to_thread(self._compact_started_at, session_id)
        if current_started_at != started_at:
            return

        session = await asyncio.to_thread(self._session_manager.get, session_id)
        if session is None or session.status != "paused":
            await asyncio.to_thread(self._clear_compact_marker, session_id, started_at)
            return

        transition = SessionStatusTransition.from_session(session)
        presentation = await self._presentation(session_id, since=started_at)
        await asyncio.to_thread(self._clear_compact_marker, session_id, started_at)
        if presentation.has_real_activity:
            await self._send_transition(transition, presentation)
            return

        await self._send_transition(
            transition,
            presentation,
            label="Compaction failed",
            event_id=f"{session_id}:compact-failed:{started_at.isoformat()}",
        )

    async def _presentation(
        self,
        session_id: str,
        *,
        since: datetime | None,
    ) -> PausePresentation:
        window = await self._transcript_reader.get_rendered_window(
            session_id,
            limit=100,
            offset=0,
            order="tail",
        )
        visible = [
            group
            for group in window.groups
            if group.role == "assistant" and not _is_compaction_group(group)
        ]
        latest = visible[-1] if visible else None
        activity = any(since is None or _as_utc(group.timestamp) >= since for group in visible)
        if latest is None:
            return PausePresentation(None, None, activity, False)

        questions = _structured_questions(latest)
        if questions:
            text = "\n\n".join(
                part for part in (latest.content.strip(), _render_questions(questions)) if part
            )
            keyboard = _question_keyboard(questions)
            return PausePresentation(text, keyboard, activity, True)

        content = latest.content.strip()
        return PausePresentation(content or None, None, activity, False)

    def _compact_started_at(self, session_id: str) -> datetime | None:
        variables = self._variables.get_variables(session_id)
        return _parse_utc(variables.get(COMPACT_NOTIFICATION_STARTED_AT_VARIABLE))

    def _clear_compact_marker(
        self,
        session_id: str,
        expected_started_at: datetime,
    ) -> None:
        if self._compact_started_at(session_id) != expected_started_at:
            return
        self._variables.set_variable(
            session_id,
            COMPACT_NOTIFICATION_STARTED_AT_VARIABLE,
            "",
        )


def _is_compaction_group(group: RenderedMessage) -> bool:
    if any(block.type == "compaction_summary" for block in group.content_blocks):
        return True
    calls = _tool_calls(group)
    return bool(calls) and all(
        _normalized_tool_name(call) in _COMPACTION_TOOL_NAMES for call in calls
    )


def _tool_calls(group: RenderedMessage) -> list[RenderedToolCall]:
    return [
        call
        for block in group.content_blocks
        if block.type == "tool_chain" and block.tool_calls
        for call in block.tool_calls
    ]


def _normalized_tool_name(call: RenderedToolCall) -> str:
    name = call.tool_name
    if _tool_basename(name) == "call_tool":
        target = call.arguments.get("tool_name")
        if isinstance(target, str):
            name = target
    return _tool_basename(name)


def _tool_basename(name: str) -> str:
    for separator in ("__", ":", "/", "."):
        if separator in name:
            name = name.rsplit(separator, 1)[-1]
    return name


def _structured_questions(group: RenderedMessage) -> list[dict[str, Any]]:
    for call in reversed(_tool_calls(group)):
        if _normalized_tool_name(call) != _QUESTION_TOOL_NAME:
            continue
        questions = call.arguments.get("questions")
        if isinstance(questions, list) and all(isinstance(item, dict) for item in questions):
            return cast(list[dict[str, Any]], questions)
    return []


def _render_questions(questions: list[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for question in questions:
        prompt = question.get("question")
        if not isinstance(prompt, str) or not prompt.strip():
            continue
        header = question.get("header")
        section = (
            f"{header.strip()}\n{prompt.strip()}"
            if isinstance(header, str) and header.strip()
            else prompt.strip()
        )
        options = question.get("options")
        if isinstance(options, list):
            rendered_options: list[str] = []
            for option in options:
                if not isinstance(option, dict):
                    continue
                label = option.get("label")
                if not isinstance(label, str) or not label.strip():
                    continue
                description = option.get("description")
                suffix = (
                    f" — {description.strip()}"
                    if isinstance(description, str) and description.strip()
                    else ""
                )
                rendered_options.append(f"• {label.strip()}{suffix}")
            if rendered_options:
                section += "\n" + "\n".join(rendered_options)
        rendered.append(section)
    return "\n\n".join(rendered)


def _question_keyboard(
    questions: list[dict[str, Any]],
) -> list[list[dict[str, str]]] | None:
    if len(questions) != 1:
        return None
    options = questions[0].get("options")
    if not isinstance(options, list):
        return None
    rows = [
        [{"text": label, "value": label}]
        for option in options
        if isinstance(option, dict)
        and isinstance((label := option.get("label")), str)
        and label.strip()
    ]
    return rows or None


def _append_reply_affordance(content: str | None) -> str:
    if not content:
        return _REPLY_AFFORDANCE
    return f"{content}\n\n{_REPLY_AFFORDANCE}"


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        logger.warning("Ignoring invalid compact notification timestamp %r", value)
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
