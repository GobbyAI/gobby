"""Reserve-then-activate state machine for tmux WebSocket attachments.

``tmux_attach`` reserves a ``streaming_id`` and acknowledges it without
touching tmux. The client's first ``tmux_resize`` activates the reservation:
that is the earliest point at which the real terminal geometry is known, so
tmux attaches exactly once at the right size, history is captured at that
width, and the PTY reader starts only after the history frame is on the wire.

Activation is awaited inline by the resize handler. ``WebSocketServer`` handles
one message at a time per connection, so awaiting here keeps this socket's
``terminal_input`` and ``tmux_detach`` queued behind activation instead of
racing a half-built bridge.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, NamedTuple, Protocol, cast

from gobby.agents.tmux.history import (
    HistoryCapture,
    HistoryCaptureError,
    capture_history,
    kill_and_reap,
)
from gobby.agents.tmux.pty_bridge import TmuxPTYBridge
from gobby.agents.tmux.session_manager import TmuxSessionManager, _exact_session_target
from gobby.config.tmux import TmuxConfig
from gobby.utils.json_helpers import json_dumps

logger = logging.getLogger(__name__)

# Resize dimensions are untrusted and they set the pane width, so an absurd
# ``cols`` would make tmux widen the pane and capture-pane allocate far past
# anything the post-capture byte cap can undo.
MAX_TERMINAL_ROWS = 1000
MAX_TERMINAL_COLS = 2000

# ``TmuxPTYBridge.attach`` returns once the subprocess exists, which is before
# tmux has necessarily registered the client and applied the pane geometry --
# capturing earlier can read the previous pane width.
CLIENT_REGISTRATION_TIMEOUT_SECONDS = 2.0
CLIENT_REGISTRATION_POLL_SECONDS = 0.05
LIST_CLIENTS_TIMEOUT_SECONDS = 2.0

STATE_RESERVED = "reserved"
STATE_ACTIVATING = "activating"


@dataclass
class PendingAttachment:
    """A reserved attachment, and later the one being activated."""

    session_name: str
    socket: str
    owner: Any
    state: str = STATE_RESERVED
    # Activation runs as part of the connection task, so cross-socket teardown
    # cannot cancel a task handle without killing the whole connection. It sets
    # this flag instead; activation observes it at its next revalidation point.
    cancelled: bool = False


class _BridgeAgent(NamedTuple):
    """The minimal shape ``PTYReaderManager.start_reader`` consumes."""

    run_id: str
    master_fd: int


class TmuxAttachHost(Protocol):
    """The slice of ``TmuxMixin`` this module drives."""

    _tmux_bridge: TmuxPTYBridge
    _tmux_pending: dict[str, PendingAttachment]
    _tmux_client_bridges: dict[Any, set[str]]

    def _get_tmux_manager(self, socket: str) -> TmuxSessionManager: ...

    def _get_tmux_config(self, socket: str) -> TmuxConfig: ...


def normalize_socket_name(value: str | None) -> str:
    """Map wire values and ``TmuxConfig.socket_name`` onto one vocabulary.

    ``BridgeInfo.socket_name`` carries the config value, which is ``""`` for the
    user's default server while the wire says ``"default"`` -- comparing them
    raw silently never matches.
    """
    return "gobby" if value == "gobby" else "default"


def valid_dimensions(rows: int, cols: int) -> bool:
    """Return whether client-supplied terminal dimensions are usable."""
    return 0 < rows <= MAX_TERMINAL_ROWS and 0 < cols <= MAX_TERMINAL_COLS


def cancel_pending(host: TmuxAttachHost, streaming_id: str) -> None:
    """Drop a reservation and signal any in-flight activation to abort."""
    pending = host._tmux_pending.pop(streaming_id, None)
    if pending is not None:
        pending.cancelled = True


def cancel_pending_for_owner(host: TmuxAttachHost, websocket: Any) -> None:
    """Drop every reservation held by a disconnecting client."""
    for streaming_id, pending in list(host._tmux_pending.items()):
        if pending.owner is websocket:
            cancel_pending(host, streaming_id)


def cancel_pending_for_session(host: TmuxAttachHost, session_name: str, socket: str) -> None:
    """Drop every reservation targeting a session that is going away."""
    for streaming_id, pending in list(host._tmux_pending.items()):
        if pending.session_name == session_name and pending.socket == socket:
            cancel_pending(host, streaming_id)


def cancel_all_pending(host: TmuxAttachHost) -> None:
    """Drop every reservation, for server shutdown."""
    for streaming_id in list(host._tmux_pending):
        cancel_pending(host, streaming_id)


def cancel_stale_reservations(
    host: TmuxAttachHost,
    session_name: str,
    socket: str,
    websocket: Any,
    *,
    exclude_streaming_id: str | None = None,
) -> None:
    """Drop this client's (or a dead client's) reservations for one session.

    A reservation left behind by a duplicate attach has no bridge, so a later
    resize naming that older id would happily build a second tmux client past
    the reap. A reservation held by a different, still-live socket is a second
    viewer-to-be and is left alone.
    """
    for streaming_id, pending in list(host._tmux_pending.items()):
        if streaming_id == exclude_streaming_id:
            continue
        if pending.session_name != session_name or pending.socket != socket:
            continue
        if pending.owner is not websocket and not _owner_is_closed(pending.owner):
            continue
        cancel_pending(host, streaming_id)
        logger.debug(
            "Cancelled stale tmux reservation %s for session '%s'", streaming_id, session_name
        )


async def reap_stale_attachments(
    host: TmuxAttachHost,
    session_name: str,
    socket: str,
    websocket: Any,
    *,
    exclude_streaming_id: str | None = None,
) -> None:
    """Tear down this client's stale bridges and reservations for one session.

    Both the reader key and the bridge key derive from a fresh uuid per attach,
    so a re-attach otherwise adds a *second* ``tmux attach-session`` client to
    the same session. tmux then sizes the session against both clients and the
    capture width stops matching the repaint width, which is what makes
    restored history wrap wrong.
    """
    from gobby.agents.pty_reader import get_pty_reader_manager

    owner_of: dict[str, Any] = {}
    for owner, bridge_ids in host._tmux_client_bridges.items():
        for bridge_id in bridge_ids:
            owner_of[bridge_id] = owner

    reader = get_pty_reader_manager()
    for streaming_id, bridge in list((await host._tmux_bridge.list_bridges()).items()):
        if streaming_id == exclude_streaming_id:
            continue
        if bridge.session_name != session_name:
            continue
        if normalize_socket_name(bridge.socket_name) != socket:
            continue
        owner = owner_of.get(streaming_id)
        # A bridge held by a different, still-live socket is a second viewer.
        # An unowned bridge is an orphan its owner's cleanup already forgot.
        if owner is not None and owner is not websocket and not _owner_is_closed(owner):
            continue
        await reader.stop_reader(streaming_id)
        await host._tmux_bridge.detach(streaming_id)
        for bridge_ids in host._tmux_client_bridges.values():
            bridge_ids.discard(streaming_id)
        cancel_pending(host, streaming_id)
        logger.debug("Reaped stale tmux bridge %s for session '%s'", streaming_id, session_name)

    cancel_stale_reservations(
        host,
        session_name,
        socket,
        websocket,
        exclude_streaming_id=exclude_streaming_id,
    )


async def activate_attachment(
    host: TmuxAttachHost,
    websocket: Any,
    streaming_id: str,
    pending: PendingAttachment,
    rows: int,
    cols: int,
) -> None:
    """Build the bridge, deliver history, then start streaming.

    Every awaited boundary revalidates: connections are separate tasks, so a
    different websocket can kill the session, reap the bridge, or stop the
    server mid-activation.
    """
    session_name = pending.session_name
    socket = pending.socket
    manager = host._get_tmux_manager(socket)
    config = host._get_tmux_config(socket)

    await reap_stale_attachments(
        host, session_name, socket, websocket, exclude_streaming_id=streaming_id
    )
    if not _still_activating(host, streaming_id, pending):
        return

    # Hide the status bar and enable mouse reporting before the tmux client's
    # first paint, so it never renders chrome the web terminal has no use for.
    try:
        await manager.set_option(session_name, "status", "off")
        await manager.set_option(session_name, "mouse", "on")
    except Exception as exc:
        logger.debug("Failed to configure tmux session '%s': %s", session_name, exc)
    if not _still_activating(host, streaming_id, pending):
        return

    try:
        master_fd = await host._tmux_bridge.attach(
            session_name=session_name,
            streaming_id=streaming_id,
            config=config,
            rows=rows,
            cols=cols,
        )
    except Exception as exc:
        logger.error("Failed to build tmux bridge for '%s': %s", session_name, exc)
        await _fail(host, websocket, streaming_id, "bridge_failed", f"Attach failed: {exc}")
        return

    # Register before the next await: a teardown landing during capture must
    # find a bridge to clean up.
    host._tmux_client_bridges.setdefault(websocket, set()).add(streaming_id)

    bridge = await host._tmux_bridge.get_bridge(streaming_id)
    if bridge is None:
        await _fail(
            host, websocket, streaming_id, "bridge_failed", "Bridge disappeared after attach"
        )
        return
    if not _still_activating(host, streaming_id, pending):
        await _teardown(host, streaming_id)
        return

    if not await _wait_for_client(manager, session_name, bridge.proc.pid):
        if not _still_activating(host, streaming_id, pending):
            await _teardown(host, streaming_id)
            return
        await _fail(
            host,
            websocket,
            streaming_id,
            "client_registration_failed",
            f"tmux client for '{session_name}' never registered",
        )
        return
    if not await _checkpoint(host, websocket, streaming_id, pending, bridge.proc):
        return

    # The kernel PTY buffer is the queue: the tmux client has been painting at
    # the client's real geometry since attach, and the reader drains it in
    # order right after the history frame, so the capture-to-stream seam has
    # no gap and no duplicate row.
    unavailable = False
    history = HistoryCapture(text="", truncated=False, dropped_bytes=0, total_bytes=0)
    try:
        history = await capture_history(manager, session_name)
    except HistoryCaptureError as exc:
        logger.warning("tmux history capture failed for '%s': %s", session_name, exc)
        unavailable = True

    if not await _checkpoint(host, websocket, streaming_id, pending, bridge.proc):
        return
    if unavailable and not await manager.has_session(session_name):
        # Nothing is left to stream, so degrading would hand the user a
        # terminal that can never produce a byte.
        await _fail(
            host,
            websocket,
            streaming_id,
            "session_missing",
            f"Session '{session_name}' disappeared during attach",
        )
        return

    try:
        await websocket.send(
            json_dumps(
                {
                    "type": "terminal_attach_history",
                    "streaming_id": streaming_id,
                    "text": history.text,
                    "truncated": history.truncated,
                    "unavailable": unavailable,
                    "dropped_bytes": history.dropped_bytes,
                    "total_bytes": history.total_bytes,
                },
                # json_dumps defaults to ensure_ascii=True, which renders every
                # ESC byte and every box-drawing glyph as a six-byte \uXXXX
                # escape. Agent CLIs are full of both.
                ensure_ascii=False,
            )
        )
    except Exception as exc:
        # The socket is broken, so a second frame on it cannot be trusted to
        # arrive; the client learns through the connection-close path.
        logger.debug("Failed to send tmux history for %s: %s", streaming_id, exc)
        host._tmux_pending.pop(streaming_id, None)
        await _teardown(host, streaming_id)
        return

    if not await _checkpoint(host, websocket, streaming_id, pending, bridge.proc):
        return

    from gobby.agents.pty_reader import get_pty_reader_manager

    reader = get_pty_reader_manager()
    if not await reader.start_reader(cast(Any, _BridgeAgent(streaming_id, master_fd))):
        await _fail(
            host,
            websocket,
            streaming_id,
            "reader_failed",
            f"PTY reader refused to start for '{session_name}'",
        )
        return
    if not await _checkpoint(host, websocket, streaming_id, pending, bridge.proc):
        return

    try:
        await manager.refresh_client(session_name)
    except Exception as exc:
        logger.debug("Post-activation refresh-client failed: %s", exc)

    if host._tmux_pending.get(streaming_id) is pending:
        del host._tmux_pending[streaming_id]


def _still_activating(host: TmuxAttachHost, streaming_id: str, pending: PendingAttachment) -> bool:
    """Return whether this activation still owns the reservation."""
    return host._tmux_pending.get(streaming_id) is pending and not pending.cancelled


async def _checkpoint(
    host: TmuxAttachHost,
    websocket: Any,
    streaming_id: str,
    pending: PendingAttachment,
    proc: asyncio.subprocess.Process,
) -> bool:
    """Revalidate after an awaited step; clean up and return False on loss."""
    if not _still_activating(host, streaming_id, pending):
        await _teardown(host, streaming_id)
        return False
    if proc.returncode is not None:
        await _fail(
            host,
            websocket,
            streaming_id,
            "bridge_exited",
            f"tmux client exited during attach (rc={proc.returncode})",
        )
        return False
    return True


async def _teardown(host: TmuxAttachHost, streaming_id: str) -> None:
    """Stop the reader, detach the bridge, and forget the ownership entry."""
    from gobby.agents.pty_reader import get_pty_reader_manager

    reader = get_pty_reader_manager()
    await reader.stop_reader(streaming_id)
    await host._tmux_bridge.detach(streaming_id)
    for bridge_ids in host._tmux_client_bridges.values():
        bridge_ids.discard(streaming_id)


async def _fail(
    host: TmuxAttachHost,
    websocket: Any,
    streaming_id: str,
    code: str,
    message: str,
) -> None:
    """Report a terminal activation failure and clean up everything it built.

    This cannot reuse the generic ``error`` frame: the web client only matches
    ``error`` against a *pending* request, and ``tmux_attach_result`` has
    already cleared that entry by activation time, so a post-ack ``error`` is
    silently swallowed and the UI stays attached to a dead stream.
    """
    host._tmux_pending.pop(streaming_id, None)
    await _teardown(host, streaming_id)
    try:
        await websocket.send(
            json_dumps(
                {
                    "type": "tmux_activation_failed",
                    "streaming_id": streaming_id,
                    "code": code,
                    "message": message,
                }
            )
        )
    except Exception as exc:
        logger.debug("Failed to deliver tmux_activation_failed for %s: %s", streaming_id, exc)


async def _wait_for_client(
    manager: TmuxSessionManager,
    session_name: str,
    pid: int,
    *,
    timeout: float = CLIENT_REGISTRATION_TIMEOUT_SECONDS,
) -> bool:
    """Poll ``list-clients`` until tmux reports our attach process as a client."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    target = str(pid)
    while True:
        if target in await _list_client_pids(manager, session_name):
            return True
        if loop.time() >= deadline:
            logger.debug("tmux client pid %s never appeared for session '%s'", target, session_name)
            return False
        await asyncio.sleep(CLIENT_REGISTRATION_POLL_SECONDS)


async def _list_client_pids(manager: TmuxSessionManager, session_name: str) -> set[str]:
    """Return the client pids tmux reports for a session, empty on any failure."""
    args = [
        *manager.base_args(),
        "list-clients",
        "-t",
        _exact_session_target(session_name),
        "-F",
        "#{client_pid}",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.debug("tmux list-clients could not start for '%s': %s", session_name, exc)
        return set()

    try:
        stdout_bytes, _stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=LIST_CLIENTS_TIMEOUT_SECONDS
        )
    except TimeoutError:
        await kill_and_reap(proc)
        return set()
    except asyncio.CancelledError:
        await kill_and_reap(proc)
        raise

    if proc.returncode:
        return set()
    decoded = (stdout_bytes or b"").decode("utf-8", errors="replace")
    return {line.strip() for line in decoded.splitlines() if line.strip()}


def _owner_is_closed(owner: Any) -> bool:
    """Return whether a websocket owner is already gone."""
    closed = getattr(owner, "closed", None)
    if isinstance(closed, bool):
        return closed
    return getattr(owner, "close_code", None) is not None
