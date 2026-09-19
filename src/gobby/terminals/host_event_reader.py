"""The gterm event-stream consumer, split out of the host manager for size.

Exit events settle terminal rows off the event loop; ``input_activity`` events reach
the sink the composition root installed and never touch the database here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from gobby.terminals.host_client import HostClient, HostManagerStopped
from gobby.terminals.host_events import (
    HostEvent,
    HostEventStream,
    InputActivityEvent,
    collect_gap_cut,
)
from gobby.terminals.host_protocol import control_socket_path

if TYPE_CHECKING:
    from gobby.terminals.host_manager import TerminalHostManager

logger = logging.getLogger(__name__)

InputActivitySink = Callable[[InputActivityEvent], None]


async def connect_event_stream(manager: TerminalHostManager, since: int | None) -> HostEventStream:
    if manager._event_connector is not None:
        return await manager._event_connector(since)
    return await HostClient.open_event_stream(
        control_socket_path(manager.socket_dir),
        manager.ensure_control_token(),
        since=since,
    )


def arm_events(manager: TerminalHostManager) -> None:
    if manager._event_task is not None:
        return
    if manager._connector is not None and manager._event_connector is None:
        return
    manager._event_task = asyncio.create_task(event_reader_loop(manager), name="gterm-host-events")


async def apply_host_event(manager: TerminalHostManager, event: HostEvent) -> None:
    if manager.last_event_epoch != event.epoch:
        return
    if event.seq <= manager.last_event_seq:
        return
    if isinstance(event, InputActivityEvent):
        sink = manager.input_activity_sink
        if sink is not None:
            try:
                sink(event)
            except Exception:
                logger.exception("input activity sink failed for terminal %s", event.terminal_id)
    else:
        terminal_manager = manager.terminal_manager
        if terminal_manager is not None:
            await asyncio.to_thread(
                terminal_manager.settle_exit,
                event.terminal_id,
                event.host_terminal_id,
            )
    manager.last_event_seq = event.seq


async def recover_event_gap(manager: TerminalHostManager, stream: HostEventStream) -> None:
    client = manager._client
    if client is None:
        raise ConnectionError("gterm control client unavailable")
    snapshot, replay = await collect_gap_cut(stream, client.list_inventory)
    await manager.reconcile(
        host_rows=list(snapshot.rows),
        host_epoch=snapshot.epoch,
        settle_indeterminate=True,
    )
    manager.last_event_epoch = snapshot.epoch
    manager.last_event_seq = snapshot.seq
    for event in replay:
        await apply_host_event(manager, event)


async def event_reader_loop(manager: TerminalHostManager) -> None:
    while not manager._stop_requested:
        stream: HostEventStream | None = None
        try:
            since = (
                manager.last_event_seq
                if manager.last_event_epoch is not None
                and manager.last_event_epoch == manager.host_epoch
                else None
            )
            previous_epoch = manager.last_event_epoch
            stream = await connect_event_stream(manager, since)
            manager._event_stream = stream
            if previous_epoch is None or stream.gap or stream.epoch != previous_epoch:
                await recover_event_gap(manager, stream)
            async for event in stream:
                if event.epoch != manager.last_event_epoch:
                    break
                await apply_host_event(manager, event)
        except asyncio.CancelledError:
            raise
        except HostManagerStopped:
            return
        except Exception as exc:
            manager.last_error = str(exc)
            if manager._stop_requested or manager.host_drained:
                return
            pid = manager.host_pid
            if not isinstance(pid, int) or pid <= 0 or not manager._pid_identity(pid):
                try:
                    await manager.ensure_restart()
                except HostManagerStopped:
                    return
            else:
                await asyncio.sleep(0.1)
        finally:
            if stream is not None:
                await stream.aclose()
            if manager._event_stream is stream:
                manager._event_stream = None


__all__ = [
    "InputActivitySink",
    "apply_host_event",
    "arm_events",
    "connect_event_stream",
    "event_reader_loop",
    "recover_event_gap",
]
