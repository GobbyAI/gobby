"""Daemon-held writer lease and attachment registry."""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from gobby.terminals.dimensions import InvalidTerminalDimensionsError, validate_dimensions
from gobby.terminals.ws_protocol import (
    PASTE_MAX_BYTES,
    TERMINAL_WS_SAFE_INTEGER_MAX,
    WRITE_SEQ_CAPACITY,
    SafeIntegerOverflowError,
)

LIFECYCLE_PUBLICATION_QUEUE_MAXSIZE = 256


class LifecyclePublicationError(RuntimeError):
    """The ordered lifecycle publisher stopped before an event settled."""


LifecyclePublisher = Callable[[dict[str, Any]], Awaitable[None]]
Viewer = Literal["web", "gclient"]
_VIEWER_RANK: dict[Viewer, int] = {"web": 2, "gclient": 1}


@dataclass(frozen=True)
class ControlResult:
    """Answer to take-control / release-control."""

    attachment_id: str
    granted: bool
    reason: str | None
    lease_generation: int


@dataclass(frozen=True)
class WriteAdmit:
    """Write-seq ledger decision."""

    ok: bool
    reason: str | None = None
    recorded_outcome: str | None = None
    join_inflight: bool = False


@dataclass(frozen=True)
class SizingDecision:
    """Runtime sizing effect selected by viewer precedence."""

    owner_viewer: Viewer | None
    rows: int | None = None
    cols: int | None = None

    @property
    def applied(self) -> bool:
        return self.rows is not None and self.cols is not None


@dataclass(frozen=True)
class ResizeAdmit:
    """Decision for one attachment's requested terminal geometry."""

    ok: bool
    reason: str | None = None
    sizing: SizingDecision | None = None

    @property
    def applied(self) -> bool:
        return self.sizing is not None and self.sizing.applied

    @property
    def owner_viewer(self) -> Viewer | None:
        return None if self.sizing is None else self.sizing.owner_viewer


@dataclass(frozen=True)
class FinalizedEvent:
    """Client-visible attachment death."""

    terminal_id: str
    attachment_id: str
    reason: str
    lease_generation: int
    sizing: SizingDecision | None = None


@dataclass(frozen=True)
class ScrollApplied:
    """Clamped host scroll offset for one attachment."""

    applied_rows: int
    max_rows: int


@dataclass
class _WriteRecord:
    kind: str
    fingerprint: str
    outcome: str | None = None
    reason: str | None = None
    inflight: bool = True


@dataclass
class _Attachment:
    attachment_id: str
    terminal_id: str
    frame_delivery: str
    viewer: Viewer = "gclient"
    geometry: tuple[int, int] | None = None
    resize_seq: int = 0
    viewport: tuple[int, int] | None = None
    scroll_offset: int = 0
    finalized: bool = False
    write_high_water: int = -1
    writes: OrderedDict[int, _WriteRecord] = field(default_factory=OrderedDict)
    message_seq: int = 0


@dataclass
class _Lease:
    holder: str | None = None
    generation: int = 0
    sizing_owner: str | None = None


@dataclass
class _LifecyclePublication:
    event: dict[str, Any]
    publisher: LifecyclePublisher
    completion: asyncio.Future[dict[str, Any]]


class TerminalLeaseRegistry:
    """Single grant point for writer authority, keyed by terminal_id."""

    def __init__(self, *, daemon_epoch: str | None = None) -> None:
        self._attachments: dict[str, _Attachment] = {}
        self._leases: dict[str, _Lease] = {}
        self._by_websocket: dict[object, set[str]] = {}
        self.daemon_epoch = str(uuid4())
        if daemon_epoch is not None:
            self.daemon_epoch = daemon_epoch
        self._lifecycle_seq = 0
        self._sizing_seq = 0
        self._lifecycle_committed_epoch = self.daemon_epoch
        self._lifecycle_committed_seq = 0
        self._lifecycle_counter_lock = threading.Lock()
        self._lifecycle_queue: deque[_LifecyclePublication] = deque()
        self._lifecycle_condition = asyncio.Condition()
        self._lifecycle_worker: asyncio.Task[None] | None = None
        self._lifecycle_current: _LifecyclePublication | None = None
        self._lifecycle_closed = False
        self._lifecycle_error: LifecyclePublicationError | None = None

    @property
    def lifecycle_worker(self) -> asyncio.Task[None] | None:
        worker = self._lifecycle_worker
        return worker if worker is not None and not worker.done() else None

    @property
    def lifecycle_queue_size(self) -> int:
        return len(self._lifecycle_queue)

    def lifecycle_snapshot(self) -> dict[str, str | int]:
        with self._lifecycle_counter_lock:
            return {
                "daemon_epoch": self._lifecycle_committed_epoch,
                "seq": self._lifecycle_committed_seq,
            }

    def next_lifecycle_seq(self) -> int:
        """Allocate a JavaScript-safe lifecycle sequence, rotating its epoch if needed."""
        _, sequence = self._next_lifecycle_stamp()
        return sequence

    def start_lifecycle_publication(self) -> None:
        """Start or restart the registry's sole lifecycle publication worker."""
        worker = self._lifecycle_worker
        if worker is not None and not worker.done():
            return
        self._lifecycle_closed = False
        self._lifecycle_error = None
        self._lifecycle_worker = asyncio.create_task(self._run_lifecycle_publication())

    async def publish_lifecycle(
        self,
        event: dict[str, Any],
        publisher: LifecyclePublisher,
    ) -> dict[str, Any]:
        """Admit one raw event and wait for its ordered publication to complete."""
        if self._lifecycle_worker is None and not self._lifecycle_closed:
            self.start_lifecycle_publication()
        completion: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        completion.add_done_callback(_consume_future_exception)
        item = _LifecyclePublication(dict(event), publisher, completion)
        async with self._lifecycle_condition:
            while (
                len(self._lifecycle_queue) >= LIFECYCLE_PUBLICATION_QUEUE_MAXSIZE
                and not self._lifecycle_closed
            ):
                await self._lifecycle_condition.wait()
            if self._lifecycle_closed:
                raise self._new_lifecycle_error()
            self._lifecycle_queue.append(item)
            self._lifecycle_condition.notify_all()
        return await asyncio.shield(completion)

    async def shutdown_lifecycle_publication(self) -> None:
        """Fence submissions, fail outstanding entries, and await worker termination."""
        error = LifecyclePublicationError("lifecycle publication stopped")
        async with self._lifecycle_condition:
            self._lifecycle_closed = True
            self._lifecycle_error = error
            queued = list(self._lifecycle_queue)
            self._lifecycle_queue.clear()
            for item in queued:
                _fail_publication(item, error)
            worker = self._lifecycle_worker
            self._lifecycle_condition.notify_all()
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        if self._lifecycle_worker is worker:
            self._lifecycle_worker = None

    def _next_lifecycle_stamp(self) -> tuple[str, int]:
        with self._lifecycle_counter_lock:
            if self._lifecycle_seq >= TERMINAL_WS_SAFE_INTEGER_MAX:
                self.daemon_epoch = str(uuid4())
                self._lifecycle_seq = 0
            self._lifecycle_seq += 1
            return self.daemon_epoch, self._lifecycle_seq

    def _commit_lifecycle(self, epoch: str, sequence: int) -> None:
        with self._lifecycle_counter_lock:
            self._lifecycle_committed_epoch = epoch
            self._lifecycle_committed_seq = sequence

    def _new_lifecycle_error(self) -> LifecyclePublicationError:
        error = self._lifecycle_error
        return LifecyclePublicationError(str(error or "lifecycle publication unavailable"))

    async def _run_lifecycle_publication(self) -> None:
        try:
            while True:
                async with self._lifecycle_condition:
                    while not self._lifecycle_queue:
                        if self._lifecycle_closed:
                            return
                        await self._lifecycle_condition.wait()
                    item = self._lifecycle_queue.popleft()
                    self._lifecycle_current = item
                    self._lifecycle_condition.notify_all()
                try:
                    epoch, sequence = self._next_lifecycle_stamp()
                    stamped = {
                        **item.event,
                        "daemon_epoch": epoch,
                        "seq": sequence,
                    }
                    await item.publisher(stamped)
                except asyncio.CancelledError:
                    _fail_publication(
                        item, LifecyclePublicationError("lifecycle publication stopped")
                    )
                    raise
                except Exception as exc:
                    await self._fault_lifecycle_publication(item, exc)
                    return
                else:
                    self._commit_lifecycle(epoch, sequence)
                    if not item.completion.done():
                        item.completion.set_result(stamped)
                finally:
                    if self._lifecycle_current is item:
                        self._lifecycle_current = None
        except asyncio.CancelledError:
            raise

    async def _fault_lifecycle_publication(
        self,
        current: _LifecyclePublication,
        cause: Exception,
    ) -> None:
        error = LifecyclePublicationError(f"lifecycle publication failed: {cause}")
        async with self._lifecycle_condition:
            self._lifecycle_closed = True
            self._lifecycle_error = error
            _fail_publication(current, error)
            queued = list(self._lifecycle_queue)
            self._lifecycle_queue.clear()
            for item in queued:
                _fail_publication(item, error)
            self._lifecycle_condition.notify_all()

    def attach(
        self,
        terminal_id: str,
        frame_delivery: str = "proxy",
        *,
        websocket: object | None = None,
        attachment_id: str | None = None,
        viewer: Viewer = "gclient",
    ) -> _Attachment:
        delivery = "direct" if frame_delivery == "direct" else "proxy"
        minted = attachment_id or secrets.token_hex(16)
        record = _Attachment(
            attachment_id=minted,
            terminal_id=terminal_id,
            frame_delivery=delivery,
            viewer=viewer,
        )
        self._attachments[minted] = record
        self._lease(terminal_id)
        if websocket is not None:
            self._by_websocket.setdefault(websocket, set()).add(minted)
        return record

    def get(self, attachment_id: str) -> _Attachment | None:
        record = self._attachments.get(attachment_id)
        if record is None or record.finalized:
            return None
        return record

    def holder(self, terminal_id: str) -> str | None:
        return self._lease(terminal_id).holder

    def generation(self, terminal_id: str) -> int:
        return self._lease(terminal_id).generation

    def take_control(
        self,
        terminal_id: str,
        attachment_id: str,
        *,
        takeover: bool = False,
    ) -> ControlResult:
        record = self.get(attachment_id)
        if record is None or record.terminal_id != terminal_id:
            return ControlResult(
                attachment_id, False, "stale_attachment", self.generation(terminal_id)
            )
        lease = self._lease(terminal_id)
        if lease.holder == attachment_id:
            return ControlResult(attachment_id, True, None, lease.generation)
        if lease.holder is not None and not takeover:
            return ControlResult(attachment_id, False, "held", lease.generation)
        self._bump(lease)
        lease.holder = attachment_id
        return ControlResult(attachment_id, True, None, lease.generation)

    def release_control(self, attachment_id: str) -> ControlResult:
        record = self.get(attachment_id)
        if record is None:
            return ControlResult(attachment_id, False, "stale_attachment", 0)
        lease = self._lease(record.terminal_id)
        if lease.holder != attachment_id:
            return ControlResult(attachment_id, False, "released", lease.generation)
        self._bump(lease)
        lease.holder = None
        return ControlResult(attachment_id, False, "released", lease.generation)

    def displaced_holder(self, terminal_id: str, new_holder: str) -> str | None:
        current = self._lease(terminal_id).holder
        if current is None or current == new_holder:
            return None
        return current

    def finalize(self, attachment_id: str, reason: str) -> FinalizedEvent | None:
        record = self._attachments.get(attachment_id)
        if record is None or record.finalized:
            return None
        lease = self._lease(record.terminal_id)
        if lease.holder == attachment_id:
            self._bump(lease)
            lease.holder = None
        record.finalized = True
        record.writes.clear()
        sizing: SizingDecision | None = None
        if lease.sizing_owner == attachment_id:
            owner = self._elect_sizing_owner(record.terminal_id)
            lease.sizing_owner = None if owner is None else owner.attachment_id
            if owner is not None and owner.geometry is not None:
                sizing = SizingDecision(owner.viewer, *owner.geometry)
        if not self._live_viewers(record.terminal_id):
            lease.sizing_owner = None
            sizing = SizingDecision(None)
        return FinalizedEvent(
            terminal_id=record.terminal_id,
            attachment_id=attachment_id,
            reason=reason,
            lease_generation=lease.generation,
            sizing=sizing,
        )

    def finalize_websocket(self, websocket: object, reason: str) -> list[FinalizedEvent]:
        events: list[FinalizedEvent] = []
        for attachment_id in list(self._by_websocket.pop(websocket, set())):
            event = self.finalize(attachment_id, reason)
            if event is not None:
                events.append(event)
        return events

    def set_viewport(self, attachment_id: str, rows: object, cols: object) -> tuple[int, int]:
        record = self._require_live(attachment_id)
        validated = validate_dimensions(rows, cols)
        record.viewport = validated
        return validated

    def viewport(self, attachment_id: str) -> tuple[int, int] | None:
        record = self._require_live(attachment_id)
        return record.viewport

    def set_scroll_offset(
        self, attachment_id: str, rows_from_live_edge: int, max_rows: int
    ) -> ScrollApplied:
        record = self._require_live(attachment_id)
        applied = max(0, min(int(rows_from_live_edge), max(0, int(max_rows))))
        record.scroll_offset = applied
        return ScrollApplied(applied_rows=applied, max_rows=max(0, int(max_rows)))

    def scroll_offset(self, attachment_id: str) -> int:
        return self._require_live(attachment_id).scroll_offset

    def resize_pty(self, attachment_id: str, rows: object, cols: object) -> ResizeAdmit:
        record = self.get(attachment_id)
        if record is None:
            return ResizeAdmit(False, "stale_attachment")
        try:
            validated = validate_dimensions(rows, cols)
        except InvalidTerminalDimensionsError:
            return ResizeAdmit(False, "invalid_dimensions")
        lease = self._lease(record.terminal_id)
        self._sizing_seq += 1
        record.geometry = validated
        record.resize_seq = self._sizing_seq
        owner = self._elect_sizing_owner(record.terminal_id)
        assert owner is not None and owner.geometry is not None
        lease.sizing_owner = owner.attachment_id
        sizing = SizingDecision(owner.viewer, *owner.geometry)
        if owner.attachment_id != attachment_id:
            sizing = SizingDecision(owner.viewer)
        return ResizeAdmit(True, sizing=sizing)

    def admit_write(
        self,
        terminal_id: str,
        *,
        attachment_id: str,
        expected_lease_generation: int | None,
        seq: object,
        kind: str,
        payload: bytes,
    ) -> WriteAdmit:
        record = self.get(attachment_id)
        if record is None or record.terminal_id != terminal_id:
            return WriteAdmit(False, "stale_attachment")
        try:
            parsed_seq = _as_seq(seq)
        except SafeIntegerOverflowError:
            return WriteAdmit(False, "safe_integer_overflow")
        lease = self._lease(terminal_id)
        if lease.holder != attachment_id or (
            expected_lease_generation is not None and expected_lease_generation != lease.generation
        ):
            return WriteAdmit(False, "held")
        fingerprint = sha256(f"{kind}:".encode() + payload).hexdigest()
        existing = record.writes.get(parsed_seq)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                return WriteAdmit(False, "write_seq_conflict")
            if existing.inflight:
                return WriteAdmit(True, join_inflight=True)
            return WriteAdmit(True, recorded_outcome=existing.outcome, reason=existing.reason)
        if parsed_seq <= record.write_high_water:
            return WriteAdmit(False, "write_seq_expired")
        inflight = sum(1 for item in record.writes.values() if item.inflight)
        if inflight >= WRITE_SEQ_CAPACITY:
            return WriteAdmit(False, "write_seq_capacity")
        while len(record.writes) >= WRITE_SEQ_CAPACITY:
            oldest_seq, oldest = next(iter(record.writes.items()))
            if oldest.inflight:
                return WriteAdmit(False, "write_seq_capacity")
            record.writes.pop(oldest_seq)
        record.write_high_water = parsed_seq
        record.writes[parsed_seq] = _WriteRecord(kind=kind, fingerprint=fingerprint)
        return WriteAdmit(True)

    def complete_write(
        self, attachment_id: str, seq: int, outcome: str, reason: str | None
    ) -> None:
        record = self._attachments.get(attachment_id)
        if record is None:
            return
        item = record.writes.get(seq)
        if item is None:
            return
        item.inflight = False
        item.outcome = outcome
        item.reason = reason

    def completed_write(self, attachment_id: str, seq: int) -> tuple[str, str | None] | None:
        record = self._attachments.get(attachment_id)
        if record is None:
            return None
        item = record.writes.get(seq)
        if item is None or item.inflight:
            return None
        return item.outcome or "delivered", item.reason

    def next_message_seq(self, attachment_id: str) -> int:
        record = self._require_live(attachment_id)
        if record.message_seq >= TERMINAL_WS_SAFE_INTEGER_MAX:
            raise SafeIntegerOverflowError()
        record.message_seq += 1
        return record.message_seq

    def _lease(self, terminal_id: str) -> _Lease:
        lease = self._leases.get(terminal_id)
        if lease is None:
            lease = _Lease()
            self._leases[terminal_id] = lease
        return lease

    def _elect_sizing_owner(self, terminal_id: str) -> _Attachment | None:
        candidates = [
            record
            for record in self._attachments.values()
            if record.terminal_id == terminal_id
            and not record.finalized
            and record.geometry is not None
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda record: (_VIEWER_RANK[record.viewer], record.resize_seq))

    def _live_viewers(self, terminal_id: str) -> list[_Attachment]:
        return [
            record
            for record in self._attachments.values()
            if record.terminal_id == terminal_id and not record.finalized
        ]

    def _bump(self, lease: _Lease) -> None:
        if lease.generation >= TERMINAL_WS_SAFE_INTEGER_MAX:
            raise SafeIntegerOverflowError()
        lease.generation += 1

    def _require_live(self, attachment_id: str) -> _Attachment:
        record = self.get(attachment_id)
        if record is None:
            raise KeyError(attachment_id)
        return record


def _as_seq(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SafeIntegerOverflowError()
    if value < 0 or value > TERMINAL_WS_SAFE_INTEGER_MAX:
        raise SafeIntegerOverflowError()
    return value


def paste_oversize(text: str) -> bool:
    return len(text.encode("utf-8")) > PASTE_MAX_BYTES


def _fail_publication(
    item: _LifecyclePublication,
    error: LifecyclePublicationError,
) -> None:
    if not item.completion.done():
        item.completion.set_exception(LifecyclePublicationError(str(error)))


def _consume_future_exception(future: asyncio.Future[dict[str, Any]]) -> None:
    if not future.cancelled():
        future.exception()
