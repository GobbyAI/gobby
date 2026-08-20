"""HTTP responses for durable attention episodes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Self, cast

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gobby.agents.attention_metadata import validate_metadata_text, validate_metadata_ttl_ms
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.tmux.text_injection import (
    AttentionInjectionError,
    inject_attention_answer_to_tmux_target,
)
from gobby.servers.routes.configuration_context import require_config_snapshot
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.attention import AttentionRosterSnapshot, AttentionState
from gobby.storage.session_models import Session
from gobby.utils.hashing import is_sha256
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

AttentionKey = Literal["enter", "escape", "tab", "up", "down"]


class AttentionAnswer(BaseModel):
    """Exactly one answer variant for an actionable prompt."""

    model_config = ConfigDict(extra="forbid")

    option: int | None = None
    text: str | None = None
    key: AttentionKey | None = None

    @field_validator("option", mode="before")
    @classmethod
    def validate_option(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("option must be an integer")
        return value

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value.encode("utf-8")) > 2048:
            raise ValueError("text must be at most 2,048 UTF-8 bytes")
        if any((ord(char) < 32 and char != "\n") or ord(char) == 127 for char in value):
            raise ValueError("text contains an unsupported control character")
        return value

    @model_validator(mode="after")
    def validate_variant(self) -> Self:
        if sum(value is not None for value in (self.option, self.text, self.key)) != 1:
            raise ValueError("answer must contain exactly one variant")
        return self


class AttentionRespondRequest(BaseModel):
    """Identity-checked response to one attention episode."""

    model_config = ConfigDict(extra="forbid")

    attention_id: str
    fingerprint: str
    answer: AttentionAnswer

    @field_validator("attention_id")
    @classmethod
    def validate_attention_id(cls, value: str) -> str:
        if not value:
            raise ValueError("attention_id is required")
        return value

    @field_validator("fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not is_sha256(value):
            raise ValueError("fingerprint must be a lowercase sha256 digest")
        return value


class AttentionSeenRequest(BaseModel):
    """Episode identity required to mark one prompt as seen."""

    model_config = ConfigDict(extra="forbid")
    attention_id: str

    @field_validator("attention_id")
    @classmethod
    def validate_attention_id(cls, value: str) -> str:
        if not value:
            raise ValueError("attention_id is required")
        return value


class AttentionMetadataRequest(BaseModel):
    """One bounded, expiring display-only metadata report."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(strict=True)
    ttl_ms: int = Field(strict=True)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return validate_metadata_text(value)

    @field_validator("ttl_ms")
    @classmethod
    def validate_ttl_ms(cls, value: int) -> int:
        return validate_metadata_ttl_ms(value)


@dataclass(frozen=True, slots=True)
class AttentionPane:
    """Resolved tmux target and its capture operation."""

    target: str
    tmux_cmd: Sequence[str]
    capture: Callable[[], Awaitable[str | None]]


@dataclass(slots=True)
class _TrackedEntryLock:
    lock: asyncio.Lock
    users: int = 0


PaneResolver = Callable[[AttentionState], Awaitable[AttentionPane | None]]
AttentionInjector = Callable[[AttentionPane, AttentionAnswer], Awaitable[None]]


def create_attention_router(
    server: HTTPServer,
    *,
    pane_resolver: PaneResolver | None = None,
    injector: AttentionInjector | None = None,
) -> APIRouter:
    """Create the attention response router with composed daemon services."""
    router = APIRouter(prefix="/api/attention", tags=["attention"])
    manager = server.services.attention_manager
    lifecycle_monitor = server.services.agent_lifecycle_monitor
    if lifecycle_monitor is not None:
        detector_root = lifecycle_monitor.prompt_detector
    elif server.services.detection_registry is not None:
        detector_root = PromptDetector(server.services.detection_registry)
    else:
        detector_root = None
    locks: dict[str, _TrackedEntryLock] = {}

    async def resolve_pane(state: AttentionState) -> AttentionPane | None:
        if pane_resolver is not None:
            return await pane_resolver(state)
        return await _resolve_attention_pane(server, state)

    async def inject_answer(pane: AttentionPane, answer: AttentionAnswer) -> None:
        if injector is not None:
            await injector(pane, answer)
            return
        await inject_attention_answer_to_tmux_target(
            pane.target,
            option=answer.option,
            text=answer.text,
            key=answer.key,
            tmux_cmd=pane.tmux_cmd,
        )

    @router.get("/roster")
    async def roster() -> dict[str, object]:
        if manager is None:
            raise HTTPException(status_code=503, detail={"code": "attention_unavailable"})
        metadata_store = getattr(server.services, "attention_metadata_store", None)
        metadata_snapshot = getattr(metadata_store, "snapshot", None)
        snapshot = await manager.snapshot_async(
            server.services.run_db,
            metadata_snapshot=metadata_snapshot if callable(metadata_snapshot) else None,
        )
        entries = await _load_roster_entries(server, snapshot)
        return {"epoch": snapshot.epoch, "seq": snapshot.seq, "entries": entries}

    @router.post("/{entry_id}/metadata")
    async def set_metadata(
        entry_id: str,
        request: AttentionMetadataRequest,
    ) -> dict[str, object]:
        metadata_store = getattr(server.services, "attention_metadata_store", None)
        if metadata_store is None:
            raise HTTPException(status_code=503, detail={"code": "attention_unavailable"})
        metadata = metadata_store.set(entry_id, request.text, request.ttl_ms)
        return {"status": "updated", "entry_id": entry_id, "metadata": metadata}

    @router.post("/{entry_id}/seen")
    async def mark_seen(entry_id: str, request: AttentionSeenRequest) -> dict[str, str]:
        if manager is None:
            raise HTTPException(status_code=503, detail={"code": "attention_unavailable"})
        current = await server.services.run_db(manager.get, entry_id)
        if current is None:
            raise HTTPException(status_code=404, detail={"code": "attention_not_found"})
        _require_seen_identity(current, request.attention_id)
        result = await manager.transition_async(
            server.services.run_db,
            entry_id,
            state="blocked",
            run_id=current.run_id,
            session_id=current.session_id,
            reason=current.reason,
            kind=current.kind,
            fingerprint=current.fingerprint,
            payload=current.payload,
            expected_attention_id=request.attention_id,
            mark_seen=True,
        )
        if not result.applied:
            if (
                result.current is not None
                and result.current.attention_id == request.attention_id
                and result.current.seen_at is not None
            ):
                return {"status": "seen", "entry_id": entry_id}
            if result.current is None:
                raise HTTPException(status_code=404, detail={"code": "attention_not_found"})
            _raise_stale_episode(result.current)
        return {"status": "seen", "entry_id": entry_id}

    @router.post("/{entry_id}/respond")
    async def respond(entry_id: str, request: AttentionRespondRequest) -> dict[str, str]:
        if manager is None:
            raise HTTPException(status_code=503, detail={"code": "attention_unavailable"})
        current = await server.services.run_db(manager.get, entry_id)
        if current is None:
            raise HTTPException(status_code=404, detail={"code": "attention_not_found"})

        tracked_lock = locks.setdefault(entry_id, _TrackedEntryLock(lock=asyncio.Lock()))
        tracked_lock.users += 1
        try:
            async with tracked_lock.lock:
                current = await server.services.run_db(manager.get, entry_id)
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "attention_not_found"})
                _require_current_identity(current, request)
                if current.kind != "actionable":
                    raise HTTPException(status_code=409, detail={"code": "not_actionable"})
                _validate_option_membership(current, request.answer)

                pane = await resolve_pane(current)
                if pane is None:
                    raise _injection_http_error(stage="none")
                pane_output = await pane.capture()
                if pane_output is None:
                    raise _injection_http_error(stage="none")

                latest = await server.services.run_db(manager.get, entry_id)
                if latest is None:
                    raise HTTPException(status_code=404, detail={"code": "attention_not_found"})
                _require_current_identity(latest, request)
                detector = await _resolve_prompt_detector(server, latest, detector_root)
                if detector is None:
                    raise HTTPException(status_code=503, detail={"code": "attention_unavailable"})
                observed_fingerprint = detector.pane_fingerprint(pane_output)
                if observed_fingerprint != request.fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "prompt_changed",
                            **_identity(latest),
                        },
                    )

                try:
                    await inject_answer(pane, request.answer)
                except AttentionInjectionError as exc:
                    if exc.stage == "none":
                        raise _injection_http_error(stage="none") from exc
                    await _retire_and_redetect(
                        server,
                        current=latest,
                        pane=pane,
                        detector=detector,
                    )
                    raise _injection_http_error(stage="partial") from exc

                cleared = await manager.transition_async(
                    server.services.run_db,
                    entry_id,
                    state=None,
                    expected_attention_id=latest.attention_id,
                    expected_fingerprint=latest.fingerprint,
                )
                if not cleared.applied:
                    raise _injection_http_error(stage="partial")
                return {"status": "accepted", "entry_id": entry_id}
        finally:
            tracked_lock.users -= 1
            if tracked_lock.users == 0 and locks.get(entry_id) is tracked_lock:
                locks.pop(entry_id, None)

    return router


async def _resolve_prompt_detector(
    server: HTTPServer,
    state: AttentionState,
    detector_root: PromptDetector | None,
) -> PromptDetector | None:
    if detector_root is None:
        return None
    if detector_root.provider_id is not None:
        return detector_root
    services = server.services
    if state.run_id is not None:
        for run in await _list_active_runs(services):
            if run.id == state.run_id:
                return detector_root.for_provider(run.provider)
    if state.session_id is not None:
        for session in await _list_live_sessions(services):
            if session.id == state.session_id:
                return detector_root.for_provider(session.source)
    return None


def _require_current_identity(
    current: AttentionState,
    request: AttentionRespondRequest,
) -> None:
    if (
        current.state != "blocked"
        or current.attention_id != request.attention_id
        or current.fingerprint != request.fingerprint
    ):
        _raise_stale_episode(current)


def _require_seen_identity(current: AttentionState, attention_id: str) -> None:
    if current.state != "blocked" or current.attention_id != attention_id:
        _raise_stale_episode(current)


def _raise_stale_episode(current: AttentionState) -> None:
    raise HTTPException(
        status_code=409,
        detail={"code": "stale_episode", **_identity(current)},
    )


def _identity(state: AttentionState) -> dict[str, str | None]:
    return {
        "attention_id": state.attention_id,
        "fingerprint": state.fingerprint,
    }


def _validate_option_membership(state: AttentionState, answer: AttentionAnswer) -> None:
    if answer.option is None:
        return
    raw_options_value = state.payload.get("options")
    raw_options: list[object] = raw_options_value if isinstance(raw_options_value, list) else []
    allowed = {
        value
        for raw in raw_options
        if isinstance(raw, Mapping)
        for value in [raw.get("option")]
        if isinstance(value, int) and not isinstance(value, bool)
    }
    if answer.option not in allowed:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_option", "options": sorted(allowed)},
        )


def _injection_http_error(*, stage: Literal["none", "partial"]) -> HTTPException:
    code = "injection_failed" if stage == "none" else "injection_indeterminate"
    return HTTPException(status_code=502, detail={"code": code, "stage": stage})


async def _retire_and_redetect(
    server: HTTPServer,
    *,
    current: AttentionState,
    pane: AttentionPane,
    detector: PromptDetector,
) -> None:
    manager = server.services.attention_manager
    if manager is None:
        return
    cleared = await manager.transition_async(
        server.services.run_db,
        current.entry_id,
        state=None,
        expected_attention_id=current.attention_id,
        expected_fingerprint=current.fingerprint,
    )
    if not cleared.applied:
        return
    pane_output = await pane.capture()
    if pane_output is None:
        return
    detected = detector.detect_prompt(pane_output)
    if detected is None:
        return
    await manager.transition_async(
        server.services.run_db,
        current.entry_id,
        state="blocked",
        run_id=current.run_id,
        session_id=current.session_id,
        reason=detected.kind,
        kind="actionable",
        fingerprint=detected.fingerprint,
        payload=detected.to_payload(),
    )


async def _load_roster_entries(
    server: HTTPServer,
    snapshot: AttentionRosterSnapshot,
) -> list[dict[str, object]]:
    """Join cursor-bounded attention with live run and session identity."""
    services = server.services
    runs_result, sessions_result = await asyncio.gather(
        _list_active_runs(services),
        _list_live_sessions(services),
    )
    runs = list(runs_result)
    sessions = list(sessions_result)
    attention = {state.entry_id: state for state in snapshot.states}
    task_cache: dict[str, dict[str, str | None] | None] = {}
    task_ids = {run.task_id for run in runs if run.task_id is not None}
    await asyncio.gather(
        *(_load_task_payload(services, task_id, task_cache) for task_id in task_ids)
    )
    entries: list[dict[str, object]] = []
    active_agent_sessions = {
        run.child_session_id for run in runs if run.child_session_id is not None
    }

    for run in runs:
        entry_id = f"run:{run.id}"
        entries.append(
            {
                "entry_id": entry_id,
                "run_id": run.id,
                "session_id": run.child_session_id,
                "lifecycle_status": run.status,
                "attention": _serialize_attention(attention.get(entry_id)),
                "task": task_cache.get(run.task_id) if run.task_id is not None else None,
                "provider": run.provider,
                "model": run.model,
                "tmux": _run_tmux_payload(server, run),
                "last_activity_at": _serialize_timestamp(run.updated_at),
                **_metadata_payload(snapshot, entry_id),
            }
        )

    for session in sessions:
        if session.id in active_agent_sessions:
            continue
        terminal_context = session.terminal_context
        if not isinstance(terminal_context, Mapping):
            continue
        pane = terminal_context.get("tmux_pane")
        if not isinstance(pane, str) or not pane:
            continue
        entry_id = f"session:{session.id}"
        entries.append(
            {
                "entry_id": entry_id,
                "run_id": None,
                "session_id": session.id,
                "lifecycle_status": session.status,
                "attention": _serialize_attention(attention.get(entry_id)),
                "task": None,
                "provider": session.source,
                "model": session.model,
                "tmux": _session_tmux_payload(terminal_context),
                "last_activity_at": _serialize_timestamp(session.updated_at),
                **_metadata_payload(snapshot, entry_id),
            }
        )
    return sorted(entries, key=lambda item: str(item["entry_id"]))


async def _list_active_runs(services: Any) -> list[AgentRun]:
    manager = LocalAgentRunManager(services.database)
    runs: list[AgentRun] = []
    offset = 0
    while True:
        page = await services.run_db(
            manager.list_active_for_machine,
            require_machine_id(),
            limit=500,
            offset=offset,
        )
        runs.extend(page)
        if len(page) < 500:
            return runs
        offset += len(page)


async def _list_live_sessions(services: Any) -> list[Session]:
    session_manager = services.session_manager
    if session_manager is None:
        return []
    sessions: list[Session] = []
    cursor_updated_at: str | None = None
    cursor_id: str | None = None
    while True:
        result = await services.run_db(
            session_manager.list,
            statuses=["active", "paused"],
            limit=500,
            cursor_updated_at=cursor_updated_at,
            cursor_id=cursor_id,
        )
        page = cast(list[Session], list(result))
        sessions.extend(page)
        if len(page) < 500:
            return sessions
        cursor_updated_at = _serialize_timestamp(page[-1].updated_at)
        cursor_id = page[-1].id


async def _load_task_payload(
    services: Any,
    task_id: str | None,
    cache: dict[str, dict[str, str | None] | None],
) -> dict[str, str | None] | None:
    if task_id is None:
        return None
    if task_id in cache:
        return cache[task_id]
    task = await services.run_db(services.task_manager.get_task, task_id)
    if task is None:
        cache[task_id] = None
        return None
    brief = task.to_brief()
    state = brief.get("state")
    current_stage = state.get("current_stage") if isinstance(state, Mapping) else None
    stage = current_stage.get("name") if isinstance(current_stage, Mapping) else None
    payload = {"id": task.id, "ref": brief.get("ref"), "stage": stage}
    cache[task_id] = payload
    return payload


def _serialize_attention(state: AttentionState | None) -> dict[str, object] | None:
    if state is None or state.state is None:
        return None
    return {
        "attention_id": state.attention_id,
        "state": state.state,
        "reason": state.reason,
        "kind": state.kind,
        "fingerprint": state.fingerprint,
        "payload": state.payload,
        "since": state.since,
        "seen_at": state.seen_at,
    }


def _run_tmux_payload(server: HTTPServer, run: Any) -> dict[str, object] | None:
    terminal_id = getattr(run, "terminal_id", None)
    if not isinstance(terminal_id, str) or not terminal_id:
        return None
    manager = getattr(server.services, "terminal_manager", None)
    row = None if manager is None else manager.get(terminal_id)
    session_name = None if row is None else row.session_name
    tmux_config = require_config_snapshot(server).active.tmux
    socket_path = getattr(tmux_config, "socket_path", None)
    return {
        "socket_path": socket_path if isinstance(socket_path, str) and socket_path else None,
        "session_name": session_name,
        "pane_pid": run.pid,
        "terminal_id": terminal_id,
    }


def _session_tmux_payload(terminal_context: Mapping[str, object]) -> dict[str, object]:
    from gobby.terminals.lookup import (
        attach_name_from_context,
        parent_pid_from_context,
        socket_path_from_context,
    )

    return {
        "socket_path": socket_path_from_context(terminal_context),
        "session_name": attach_name_from_context(terminal_context),
        "parent_pid": parent_pid_from_context(terminal_context),
    }


def _metadata_payload(
    snapshot: AttentionRosterSnapshot,
    entry_id: str,
) -> dict[str, object]:
    metadata = snapshot.metadata.get(entry_id)
    return {"metadata": dict(metadata)} if metadata is not None else {}


def _serialize_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def _resolve_attention_pane(
    server: HTTPServer,
    state: AttentionState,
) -> AttentionPane | None:
    services = server.services
    session_manager = services.session_manager
    manager = getattr(services, "terminal_manager", None)
    registry = getattr(services, "terminal_runtime_registry", None)
    if state.session_id is not None and manager is not None and registry is not None:
        row = manager.get_live_for_session(state.session_id)
        if row is not None:
            runtime = registry.resolve(row.backend)

            async def capture_session_pane() -> str | None:
                snapshot = await runtime.snapshot(row, 15)
                return snapshot.text

            return AttentionPane(
                target=row.id,
                tmux_cmd=(),
                capture=capture_session_pane,
            )

    agent_runner = services.agent_runner
    if state.run_id is None or agent_runner is None:
        return None
    run = await services.run_db(agent_runner.get_run, state.run_id)
    if run is None or not run.terminal_id or manager is None or registry is None:
        return None
    row = manager.get(run.terminal_id)
    if row is None:
        return None
    runtime = registry.resolve(row.backend)

    async def capture_run_pane() -> str | None:
        snapshot = await runtime.snapshot(row, 15)
        return snapshot.text

    return AttentionPane(
        target=row.id,
        tmux_cmd=(),
        capture=capture_run_pane,
    )
