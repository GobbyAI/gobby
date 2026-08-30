"""Daemon-held writer lease and attachment registry."""

from __future__ import annotations

import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256

from gobby.terminals.dimensions import InvalidTerminalDimensionsError, validate_dimensions
from gobby.terminals.ws_protocol import (
    PASTE_MAX_BYTES,
    TERMINAL_WS_SAFE_INTEGER_MAX,
    WRITE_SEQ_CAPACITY,
    SafeIntegerOverflowError,
)


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
class FinalizedEvent:
    """Client-visible attachment death."""

    terminal_id: str
    attachment_id: str
    reason: str
    lease_generation: int


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


class TerminalLeaseRegistry:
    """Single grant point for writer authority, keyed by terminal_id."""

    def __init__(self) -> None:
        self._attachments: dict[str, _Attachment] = {}
        self._leases: dict[str, _Lease] = {}
        self._by_websocket: dict[object, set[str]] = {}

    def attach(
        self,
        terminal_id: str,
        frame_delivery: str = "proxy",
        *,
        websocket: object | None = None,
        attachment_id: str | None = None,
    ) -> _Attachment:
        delivery = "direct" if frame_delivery == "direct" else "proxy"
        minted = attachment_id or secrets.token_hex(16)
        record = _Attachment(
            attachment_id=minted,
            terminal_id=terminal_id,
            frame_delivery=delivery,
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
        return FinalizedEvent(
            terminal_id=record.terminal_id,
            attachment_id=attachment_id,
            reason=reason,
            lease_generation=lease.generation,
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

    def resize_pty(self, attachment_id: str, rows: object, cols: object) -> WriteAdmit:
        record = self.get(attachment_id)
        if record is None:
            return WriteAdmit(False, "stale_attachment")
        try:
            validate_dimensions(rows, cols)
        except InvalidTerminalDimensionsError:
            return WriteAdmit(False, "invalid_dimensions")
        lease = self._lease(record.terminal_id)
        if lease.holder != attachment_id:
            return WriteAdmit(False, "held")
        return WriteAdmit(True)

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
