"""Typed lifecycle events carried by a dedicated gterm control connection."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from gobby.terminals.host_protocol import HostListRow

InputKind = Literal["input", "paste"]
InterruptKind = Literal["esc", "ctrl_c"]


@dataclass(frozen=True)
class HostInventorySnapshot:
    rows: tuple[HostListRow, ...]
    epoch: str
    seq: int


@dataclass(frozen=True)
class TerminalExitedEvent:
    terminal_id: str
    host_terminal_id: str
    exit_code: int | None
    epoch: str
    seq: int


@dataclass(frozen=True)
class InputActivityEvent:
    """One accepted ``Input``/``Paste`` on a granted frame stream, without its payload."""

    terminal_id: str
    host_terminal_id: str
    attachment_id: str
    kind: InputKind
    bytes: int
    interrupt: InterruptKind | None
    epoch: str
    seq: int


HostEvent = TerminalExitedEvent | InputActivityEvent
GAP_BUFFER_ENTRIES = 4_096
GAP_BUFFER_BYTES = 4 * 1024 * 1024


def _input_kind(value: object) -> InputKind:
    if value == "input":
        return "input"
    if value == "paste":
        return "paste"
    raise ValueError(f"unknown input_activity kind: {value!r}")


def _interrupt_kind(value: object) -> InterruptKind | None:
    if value is None:
        return None
    if value == "esc":
        return "esc"
    if value == "ctrl_c":
        return "ctrl_c"
    raise ValueError(f"unknown input_activity interrupt: {value!r}")


def decode_host_event(payload: dict[str, Any]) -> HostEvent:
    event = str(payload.get("event", ""))
    if event == "terminal_exited":
        return TerminalExitedEvent(
            terminal_id=str(payload["terminal_id"]),
            host_terminal_id=str(payload["host_terminal_id"]),
            exit_code=(int(payload["exit_code"]) if payload.get("exit_code") is not None else None),
            epoch=str(payload["epoch"]),
            seq=int(payload["seq"]),
        )
    if event == "input_activity":
        return InputActivityEvent(
            terminal_id=str(payload["terminal_id"]),
            host_terminal_id=str(payload["host_terminal_id"]),
            attachment_id=str(payload["attachment_id"]),
            kind=_input_kind(payload["kind"]),
            bytes=int(payload["bytes"]),
            interrupt=_interrupt_kind(payload.get("interrupt")),
            epoch=str(payload["epoch"]),
            seq=int(payload["seq"]),
        )
    raise ValueError(f"unknown host event: {event}")


class HostEventStream(AsyncIterator[HostEvent]):
    def __init__(
        self,
        client: Any,
        *,
        epoch: str,
        seq: int,
        gap: bool,
    ) -> None:
        self._client = client
        self.epoch = epoch
        self.seq = seq
        self.gap = gap

    def __aiter__(self) -> HostEventStream:
        return self

    async def __anext__(self) -> HostEvent:
        payload = await self._client._next_event_payload()
        if payload is None:
            raise StopAsyncIteration
        return decode_host_event(payload)

    async def aclose(self) -> None:
        await self._client.close()


async def collect_gap_cut(
    stream: HostEventStream,
    fetch_inventory: Callable[[], Awaitable[HostInventorySnapshot]],
) -> tuple[HostInventorySnapshot, list[HostEvent]]:
    """Buffer a subscribed stream around an inventory cut, repeating after overflow."""
    while True:
        buffered: list[HostEvent] = []
        buffered_bytes = 0
        overflowed = False
        inventory_task: asyncio.Future[HostInventorySnapshot] = asyncio.ensure_future(
            fetch_inventory()
        )
        next_event: asyncio.Task[HostEvent] | None = None
        try:
            while not inventory_task.done():
                next_event = asyncio.create_task(anext(stream))
                pending: set[asyncio.Future[HostInventorySnapshot] | asyncio.Task[HostEvent]] = {
                    inventory_task,
                    next_event,
                }
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if next_event in done:
                    event = next_event.result()
                    event_bytes = len(repr(event).encode("utf-8"))
                    if (
                        len(buffered) >= GAP_BUFFER_ENTRIES
                        or buffered_bytes + event_bytes > GAP_BUFFER_BYTES
                    ):
                        buffered.clear()
                        buffered_bytes = 0
                        overflowed = True
                    elif not overflowed:
                        buffered.append(event)
                        buffered_bytes += event_bytes
                    next_event = None
            snapshot = await inventory_task
            if next_event is not None:
                if next_event.done():
                    event = next_event.result()
                    event_bytes = len(repr(event).encode("utf-8"))
                    if (
                        len(buffered) >= GAP_BUFFER_ENTRIES
                        or buffered_bytes + event_bytes > GAP_BUFFER_BYTES
                    ):
                        buffered.clear()
                        overflowed = True
                    elif not overflowed:
                        buffered.append(event)
                else:
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
        except BaseException:
            inventory_task.cancel()
            if next_event is not None:
                next_event.cancel()
            await asyncio.gather(
                inventory_task,
                *([next_event] if next_event is not None else []),
                return_exceptions=True,
            )
            raise
        if overflowed:
            continue
        replay = sorted(
            (
                event
                for event in buffered
                if event.epoch == snapshot.epoch and event.seq > snapshot.seq
            ),
            key=lambda event: event.seq,
        )
        return snapshot, replay
