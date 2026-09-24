"""HTTP responses for durable attention episodes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal, Self

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gobby.agents.attention_metadata import validate_metadata_text, validate_metadata_ttl_ms
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.tmux.text_injection import AttentionInjectionError
from gobby.servers.routes.configuration_context import require_config_snapshot
from gobby.storage.attention import (
    AttentionRosterRow,
    AttentionRosterSnapshot,
    AttentionRosterTerminal,
    AttentionState,
)
from gobby.storage.sessions import LIVE_SESSION_STATUS_ORDER
from gobby.storage.terminals import attach_locator_for_terminal
from gobby.terminals.runtime import (
    Delivered,
    IndeterminateWrite,
    TerminalWriteError,
)
from gobby.terminals.write_coordinator import WriteRequest
from gobby.utils.hashing import is_sha256
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

AttentionKey = Literal["enter", "escape", "tab", "up", "down"]
ATTENTION_ROSTER_CACHE_TTL_SECONDS = 1.0

logger = logging.getLogger(__name__)


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


@dataclass(slots=True)
class _RosterCache:
    lock: asyncio.Lock
    cursor: tuple[str, int] | None = None
    created_at: float = 0.0
    payload: dict[str, object] | None = None


@dataclass(slots=True)
class _RosterProfile:
    executor_wait_seconds: float = 0.0
    query_seconds: float = 0.0


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
    roster_cache = _RosterCache(lock=asyncio.Lock())

    async def resolve_pane(state: AttentionState) -> AttentionPane | None:
        if pane_resolver is not None:
            return await pane_resolver(state)
        return await _resolve_attention_pane(server, state)

    async def inject_answer(
        pane: AttentionPane,
        answer: AttentionAnswer,
        state: AttentionState,
    ) -> None:
        if injector is not None:
            await injector(pane, answer)
            return
        coordinator = getattr(server.services, "write_coordinator", None)
        if coordinator is None:
            raise AttentionInjectionError(stage="none")
        kind: Literal["text", "key"] = "key" if answer.key is not None else "text"
        payload = (
            answer.key
            if answer.key is not None
            else str(answer.option)
            if answer.option is not None
            else answer.text or ""
        )
        try:
            result = await coordinator.write(
                WriteRequest(
                    terminal_id=pane.target,
                    action_key=f"attention-respond:{state.attention_id}",
                    origin="attention",
                    kind=kind,
                    payload=payload,
                    submit=kind == "text",
                )
            )
        except TerminalWriteError as exc:
            raise AttentionInjectionError(stage=exc.stage) from exc
        except KeyError as exc:
            raise AttentionInjectionError(stage="none") from exc
        if isinstance(result, IndeterminateWrite):
            raise AttentionInjectionError(stage="partial")
        if not isinstance(result, Delivered):
            raise AttentionInjectionError(stage="none")

    @router.get("/roster")
    async def roster() -> dict[str, object]:
        if manager is None:
            raise HTTPException(status_code=503, detail={"code": "attention_unavailable"})
        started_at = perf_counter()
        profile = _RosterProfile()
        async with roster_cache.lock:
            async with manager.ordering.lock:
                with manager.ordering.synchronized():
                    cursor = (manager.epoch, manager.seq)
                    cache_checked_at = perf_counter()
                    if (
                        roster_cache.payload is not None
                        and roster_cache.cursor == cursor
                        and cache_checked_at - roster_cache.created_at
                        < ATTENTION_ROSTER_CACHE_TTL_SECONDS
                    ):
                        _log_roster_profile(
                            profile,
                            cache_hit=True,
                            assembly_seconds=0.0,
                            total_seconds=perf_counter() - started_at,
                            entry_count=_entry_count(roster_cache.payload),
                        )
                        return roster_cache.payload

            async def profiled_run_db(
                function: Callable[..., Any], *args: Any, **kwargs: Any
            ) -> Any:
                return await _profiled_run_db(
                    server.services.run_db,
                    profile,
                    function,
                    *args,
                    **kwargs,
                )

            metadata_store = getattr(server.services, "attention_metadata_store", None)
            metadata_snapshot = getattr(metadata_store, "snapshot", None)
            snapshot = await manager.snapshot_async(
                profiled_run_db,
                metadata_snapshot=metadata_snapshot if callable(metadata_snapshot) else None,
            )
            rows = await profiled_run_db(
                manager.load_roster_rows,
                require_machine_id(),
                live_session_statuses=LIVE_SESSION_STATUS_ORDER,
            )
            resolver = getattr(server.services, "provider_capability_resolver", None)
            # The capability catalog reads the database, so names resolve off the loop.
            display_names = (
                {}
                if resolver is None
                else await profiled_run_db(_model_display_names, resolver, rows)
            )
            assembly_started_at = perf_counter()
            entries = _load_roster_entries(server, snapshot, rows, display_names)
            assembly_seconds = perf_counter() - assembly_started_at
            payload: dict[str, object] = {
                "epoch": snapshot.epoch,
                "seq": snapshot.seq,
                "entries": entries,
            }
            if (manager.epoch, manager.seq) == (snapshot.epoch, snapshot.seq):
                roster_cache.cursor = (snapshot.epoch, snapshot.seq)
                roster_cache.created_at = perf_counter()
                roster_cache.payload = payload
            _log_roster_profile(
                profile,
                cache_hit=False,
                assembly_seconds=assembly_seconds,
                total_seconds=perf_counter() - started_at,
                entry_count=len(entries),
            )
            return payload

    @router.post("/{entry_id}/metadata")
    async def set_metadata(
        entry_id: str,
        request: AttentionMetadataRequest,
    ) -> dict[str, object]:
        metadata_store = getattr(server.services, "attention_metadata_store", None)
        if metadata_store is None:
            raise HTTPException(status_code=503, detail={"code": "attention_unavailable"})
        metadata = metadata_store.set(entry_id, request.text, request.ttl_ms)
        async with roster_cache.lock:
            roster_cache.payload = None
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

                claimed_before_injection = injector is None
                if claimed_before_injection:
                    claimed = await manager.transition_async(
                        server.services.run_db,
                        entry_id,
                        state=None,
                        expected_attention_id=latest.attention_id,
                        expected_fingerprint=latest.fingerprint,
                    )
                    if not claimed.applied:
                        if claimed.current is None:
                            raise HTTPException(
                                status_code=404,
                                detail={"code": "attention_not_found"},
                            )
                        _raise_stale_episode(claimed.current)

                try:
                    await inject_answer(pane, request.answer, latest)
                except AttentionInjectionError as exc:
                    if claimed_before_injection:
                        await _redetect_retired_attention(
                            server,
                            current=latest,
                            pane=pane,
                            detector=detector,
                        )
                        raise _injection_http_error(stage=exc.stage) from exc
                    if exc.stage == "none":
                        raise _injection_http_error(stage="none") from exc
                    await _retire_and_redetect(
                        server,
                        current=latest,
                        pane=pane,
                        detector=detector,
                    )
                    raise _injection_http_error(stage="partial") from exc

                if claimed_before_injection:
                    return {"status": "accepted", "entry_id": entry_id}
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
    manager = services.attention_manager
    if manager is None:
        return None
    rows = await services.run_db(
        manager.load_roster_rows,
        require_machine_id(),
        live_session_statuses=LIVE_SESSION_STATUS_ORDER,
    )
    for row in rows:
        if (row.kind == "run" and row.source_id == state.run_id) or (
            row.kind == "session" and row.source_id == state.session_id
        ):
            return detector_root.for_provider(row.provider)
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


async def _profiled_run_db(
    run_db: Callable[..., Awaitable[Any]],
    profile: _RosterProfile,
    function: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run one database operation while separating executor wait from query time."""
    query_seconds = 0.0

    def measured() -> Any:
        nonlocal query_seconds
        query_started_at = perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            query_seconds = perf_counter() - query_started_at

    wait_started_at = perf_counter()
    try:
        return await run_db(measured)
    finally:
        elapsed = perf_counter() - wait_started_at
        profile.query_seconds += query_seconds
        profile.executor_wait_seconds += max(0.0, elapsed - query_seconds)


def _log_roster_profile(
    profile: _RosterProfile,
    *,
    cache_hit: bool,
    assembly_seconds: float,
    total_seconds: float,
    entry_count: int,
) -> None:
    logger.debug(
        "Attention roster profile cache_hit=%s entries=%d executor_wait_ms=%.3f "
        "query_ms=%.3f assembly_ms=%.3f total_ms=%.3f",
        cache_hit,
        entry_count,
        profile.executor_wait_seconds * 1000,
        profile.query_seconds * 1000,
        assembly_seconds * 1000,
        total_seconds * 1000,
    )


def _entry_count(payload: Mapping[str, object]) -> int:
    entries = payload.get("entries")
    return len(entries) if isinstance(entries, list) else 0


def _load_roster_entries(
    server: HTTPServer,
    snapshot: AttentionRosterSnapshot,
    rows: Sequence[AttentionRosterRow],
    display_names: Mapping[tuple[str | None, str | None], str | None],
) -> list[dict[str, object]]:
    """Join cursor-bounded attention with one bounded identity query."""
    runs = [row for row in rows if row.kind == "run"]
    sessions = [row for row in rows if row.kind == "session"]
    attention = {state.entry_id: state for state in snapshot.states}
    entries: list[dict[str, object]] = []
    active_agent_sessions = {run.session_id for run in runs if run.session_id is not None}

    for run in runs:
        entry_id = f"run:{run.source_id}"
        task = None
        if run.task_id is not None and run.task_ref is not None:
            task = {"id": run.task_id, "ref": run.task_ref, "stage": run.task_stage}
        entries.append(
            {
                "entry_id": entry_id,
                "run_id": run.source_id,
                "session_id": run.session_id,
                "lifecycle_status": run.lifecycle_status,
                "attention": _serialize_attention(attention.get(entry_id)),
                "task": task,
                "provider": run.provider,
                "model": run.model,
                "model_display_name": display_names.get((run.provider, run.model)),
                "terminal": _terminal_block(server, run.terminal),
                "tmux": _run_tmux_payload(server, run),
                "last_activity_at": _serialize_timestamp(run.updated_at),
                **_metadata_payload(snapshot, entry_id),
            }
        )

    for session in sessions:
        if session.source_id in active_agent_sessions:
            continue
        terminal_context = session.terminal_context
        terminal = _terminal_block(server, session.terminal)
        pane = terminal_context.get("tmux_pane")
        if terminal is None and (not isinstance(pane, str) or not pane):
            continue
        entry_id = f"session:{session.source_id}"
        entries.append(
            {
                "entry_id": entry_id,
                "run_id": None,
                "session_id": session.session_id,
                "lifecycle_status": session.lifecycle_status,
                "attention": _serialize_attention(attention.get(entry_id)),
                "task": None,
                "provider": session.provider,
                "model": session.model,
                "model_display_name": display_names.get((session.provider, session.model)),
                "terminal": terminal,
                "tmux": _session_tmux_payload(terminal_context),
                "last_activity_at": _serialize_timestamp(session.updated_at),
                **_metadata_payload(snapshot, entry_id),
            }
        )
    return sorted(entries, key=lambda item: str(item["entry_id"]))


def _model_display_names(
    resolver: Any,
    rows: Sequence[AttentionRosterRow],
) -> dict[tuple[str | None, str | None], str | None]:
    """Each distinct model's name as its provider prints it, when the catalog has it."""
    names: dict[tuple[str | None, str | None], str | None] = {}
    for row in rows:
        key = (row.provider, row.model)
        if not row.provider or not row.model or key in names:
            continue
        capability = resolver.find_model(row.provider, row.model)
        names[key] = None if capability is None else capability.display_name
    return names


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


def _terminal_block(
    server: HTTPServer,
    terminal: AttentionRosterTerminal | None,
) -> dict[str, object] | None:
    if terminal is None:
        return None
    manager = getattr(server.services, "terminal_manager", None)
    if manager is None:
        return None
    try:
        attach = attach_locator_for_terminal(
            terminal,
            live_host_epoch=terminal.host_epoch or "",
            socket_dir=Path.home() / ".gobby",
        )
    except Exception:
        attach = None
    return {
        "terminal_id": terminal.id,
        "backend": terminal.backend,
        "state": terminal.state,
        "attach": None if attach is None else asdict(attach),
    }


def _run_tmux_payload(
    server: HTTPServer,
    run: AttentionRosterRow,
) -> dict[str, object] | None:
    if run.terminal_id is None:
        return None
    terminal = getattr(run, "terminal", None)
    session_name = None if terminal is None else terminal.session_name
    tmux_config = require_config_snapshot(server).active.tmux
    socket_path = getattr(tmux_config, "socket_path", None)
    return {
        "socket_path": socket_path if isinstance(socket_path, str) and socket_path else None,
        "session_name": session_name,
        "pane_pid": run.pid,
        "terminal_id": run.terminal_id,
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


async def _redetect_retired_attention(
    server: HTTPServer,
    *,
    current: AttentionState,
    pane: AttentionPane,
    detector: PromptDetector,
) -> None:
    attention_manager = server.services.attention_manager
    if attention_manager is None:
        return
    latest = await server.services.run_db(attention_manager.get, current.entry_id)
    if latest is None or latest.state is not None:
        return
    pane_output = await pane.capture()
    if pane_output is None:
        return
    detected = detector.detect_prompt(pane_output)
    if detected is None:
        return
    await attention_manager.transition_async(
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


async def _resolve_attention_pane(
    server: HTTPServer,
    state: AttentionState,
) -> AttentionPane | None:
    services = server.services
    manager = getattr(services, "terminal_manager", None)
    registry = getattr(services, "terminal_runtime_registry", None)
    if state.session_id is not None and manager is not None and registry is not None:
        row = manager.get_live_for_session(state.session_id)
        if row is not None:
            runtime = registry.resolve(row.backend)

            async def capture_session_pane() -> str | None:
                snapshot = await runtime.snapshot(row, 15)
                text = snapshot.text
                return text if isinstance(text, str) else None

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
        text = snapshot.text
        return text if isinstance(text, str) else None

    return AttentionPane(
        target=row.id,
        tmux_cmd=(),
        capture=capture_run_pane,
    )
