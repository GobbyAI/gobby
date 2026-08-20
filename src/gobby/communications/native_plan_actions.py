"""Live native-CLI plan menus exposed as guarded communications actions."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from gobby.adapters.plan_keystrokes import (
    DEFAULT_PLAN_KEYSTROKES,
    PlanKeystrokeRegistry,
    SupportsSendKeys,
    dispatch_plan_keystrokes,
)
from gobby.agents.prompt_detector import PromptDetector
from gobby.terminals.lookup import manager_for_terminal_context

if TYPE_CHECKING:
    from gobby.agents.detection.provider import DetectionRegistry
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager

_CALLBACK_PREFIX = "native-plan:"
_MAX_BUTTON_LABEL_CHARS = 64
_PANE_CAPTURE_LINES = 30

NativePlanDispatchResult = Literal["sent", "stale", "unavailable", "failed"]


class NativePlanTmux(SupportsSendKeys, Protocol):
    async def capture_pane(self, session_name: str, lines: int = ...) -> str | None: ...

    async def dispatch_keys(
        self, session_name: str, keys: str, *, literal: bool = ...
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class NativePlanChoice:
    option: int
    label: str


@dataclass(frozen=True, slots=True)
class NativePlanMenuSnapshot:
    fingerprint: str
    choices: tuple[NativePlanChoice, ...]


@dataclass(frozen=True, slots=True)
class _CapturedMenu:
    session: Session
    tmux: NativePlanTmux | None
    pane_id: str
    pane_text: str


def encode_native_plan_option(option: int) -> str:
    """Encode a native menu option for Telegram's opaque callback registry."""
    if isinstance(option, bool) or option < 1:
        raise ValueError("native plan option must be a positive integer")
    return f"{_CALLBACK_PREFIX}{option}"


def decode_native_plan_option(value: object) -> int | None:
    """Decode a native menu callback value."""
    if not isinstance(value, str) or not value.startswith(_CALLBACK_PREFIX):
        return None
    raw = value.removeprefix(_CALLBACK_PREFIX)
    if not raw.isascii() or not raw.isdigit():
        return None
    option = int(raw)
    return option if option > 0 else None


class NativePlanActionService:
    """Read and dispatch exact live provider menus for paused terminal sessions."""

    def __init__(
        self,
        session_manager: SessionManager,
        detection_registry: DetectionRegistry,
        *,
        keystrokes: PlanKeystrokeRegistry = DEFAULT_PLAN_KEYSTROKES,
        terminal_manager: Any | None = None,
        terminal_runtime_registry: Any | None = None,
        write_coordinator: Any | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._detection_registry = detection_registry
        self._keystrokes = keystrokes
        self._terminal_manager = terminal_manager
        self._terminal_runtime_registry = terminal_runtime_registry
        self._write_coordinator = write_coordinator

    async def get_menu(self, session_id: str) -> NativePlanMenuSnapshot | None:
        """Return exact routable choices from the session's current native menu."""
        captured = await self._capture(session_id)
        return self._snapshot(captured) if captured is not None else None

    async def dispatch(
        self,
        session_id: str,
        *,
        option: int,
        expected_fingerprint: str,
    ) -> NativePlanDispatchResult:
        """Dispatch one choice after revalidating session, pane, prompt, and fingerprint."""
        captured = await self._capture(session_id)
        if captured is None:
            return "unavailable"
        snapshot = self._snapshot(captured)
        if snapshot is None or snapshot.fingerprint != expected_fingerprint:
            return "stale"
        if option not in {choice.option for choice in snapshot.choices}:
            return "stale"
        sequence = self._keystrokes.resolve_native_option_for_pane(
            captured.session.source,
            option,
            captured.pane_text,
        )
        if sequence is None:
            return "stale"
        coordinator = self._write_coordinator
        manager = self._terminal_manager
        if coordinator is not None and manager is not None:
            from gobby.terminals.runtime import Delivered
            from gobby.terminals.write_coordinator import WriteRequest

            terminal_id = getattr(captured.session, "terminal_id", None)
            terminal = manager.get(terminal_id) if isinstance(terminal_id, str) else None
            if terminal is None:
                terminal = manager.get_live_for_session(session_id)
            if terminal is None:
                return "unavailable"
            from gobby.terminals.runtime import is_named_key
            from gobby.terminals.write_coordinator import SequenceDelay

            steps: list[WriteRequest | SequenceDelay] = []
            for index, stroke in enumerate(sequence.strokes):
                if index:
                    steps.append(SequenceDelay(seconds=sequence.settle_seconds))
                if stroke.literal:
                    kind: Literal["text", "key"] = "text"
                    payload = stroke.keys
                elif is_named_key(stroke.keys.lower()):
                    kind = "key"
                    payload = stroke.keys.lower()
                else:
                    kind = "text"
                    payload = stroke.keys
                steps.append(
                    WriteRequest(
                        terminal_id=terminal.id,
                        action_key=f"native-plan:{session_id}:{option}",
                        origin="automatic",
                        kind=kind,
                        payload=payload,
                    )
                )
            outcome = await coordinator.run_sequence(
                terminal.id,
                action_key=f"native-plan:{session_id}:{option}",
                origin="automatic",
                steps=steps,
            )
            return "sent" if isinstance(outcome, Delivered) else "failed"
        if captured.tmux is None:
            return "failed"
        sent = await dispatch_plan_keystrokes(
            captured.tmux,
            captured.pane_id,
            sequence,
        )
        return "sent" if sent else "failed"

    async def _capture(self, session_id: str) -> _CapturedMenu | None:
        session = await asyncio.to_thread(self._session_manager.get, session_id)
        if session is None or session.status != "paused":
            return None
        manager = self._terminal_manager
        registry = self._terminal_runtime_registry
        if manager is not None and registry is not None:
            terminal_id = getattr(session, "terminal_id", None)
            terminal = manager.get(terminal_id) if isinstance(terminal_id, str) else None
            if terminal is None:
                terminal = manager.get_live_for_session(session_id)
            if terminal is None:
                return None
            runtime = registry.resolve(terminal.backend)
            snapshot = await runtime.snapshot(terminal, _PANE_CAPTURE_LINES)
            pane_text = snapshot.text
            if not pane_text:
                return None
            return _CapturedMenu(
                session=session,
                tmux=None,
                pane_id=terminal.id,
                pane_text=pane_text,
            )
        terminal_context = getattr(session, "terminal_context", None)
        if not isinstance(terminal_context, Mapping):
            return None
        pane_id = terminal_context.get("tmux_pane")
        if not isinstance(pane_id, str) or not pane_id:
            return None
        tmux = manager_for_terminal_context(terminal_context)
        pane_text = await tmux.snapshot_lines(pane_id, lines=_PANE_CAPTURE_LINES)
        if not pane_text:
            return None
        return _CapturedMenu(
            session=session,
            tmux=tmux,
            pane_id=pane_id,
            pane_text=pane_text,
        )

    def _snapshot(self, captured: _CapturedMenu) -> NativePlanMenuSnapshot | None:
        source = captured.session.source
        if not self._keystrokes.has_source(source):
            return None
        prompt = PromptDetector(self._detection_registry, source).prompt_payload(
            captured.pane_text,
            kind="approval",
        )
        choices: list[NativePlanChoice] = []
        for raw in prompt.options:
            option = raw.get("option")
            label = raw.get("label")
            if (
                not isinstance(option, int)
                or isinstance(option, bool)
                or not isinstance(label, str)
                or not label.strip()
                or self._keystrokes.resolve_native_option_for_pane(
                    source,
                    option,
                    captured.pane_text,
                )
                is None
            ):
                continue
            choices.append(
                NativePlanChoice(
                    option=option,
                    label=_button_label(label),
                )
            )
        if not choices:
            return None
        return NativePlanMenuSnapshot(
            fingerprint=prompt.fingerprint,
            choices=tuple(choices),
        )


def _button_label(label: str) -> str:
    normalized = " ".join(label.split())
    if len(normalized) <= _MAX_BUTTON_LABEL_CHARS:
        return normalized
    return f"{normalized[: _MAX_BUTTON_LABEL_CHARS - 1].rstrip()}…"


__all__ = [
    "NativePlanActionService",
    "NativePlanChoice",
    "NativePlanDispatchResult",
    "NativePlanMenuSnapshot",
    "decode_native_plan_option",
    "encode_native_plan_option",
]
