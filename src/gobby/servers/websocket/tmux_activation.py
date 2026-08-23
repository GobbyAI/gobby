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
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, NamedTuple, Protocol, cast

from gobby.agents.tmux.history import (
    CAPTURE_TIMEOUT_SECONDS,
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

# One deadline for the whole activation. Activation is awaited inline and the
# connection dispatches one message at a time, so everything the user does next
# queues behind it -- and the web client abandons a tmux request after 10s,
# reporting the timeout against *that* request rather than the attach. The
# individual steps below are each bounded, but their caps sum well past 10s, so
# the total is what has to be bounded. 6s leaves the queued request room to be
# served and answered inside the client's deadline.
ACTIVATION_BUDGET_SECONDS = 6.0
# Delivering the capture -- the history frame, the reader, the repaint -- has
# to fit in whatever the capture leaves behind. History is the one degradable
# step, so it is the one that yields when time is short.
ACTIVATION_TAIL_RESERVE_SECONDS = 1.5
# Every step whose own worst case carries no meaning beyond "tmux is wedged".
# The budget is the real bound; this only stops one step eating all of it.
TMUX_STEP_TIMEOUT_SECONDS = 2.0

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


class ActivationBudgetExpired(Exception):
    """The activation deadline ran out, or a step overran its share of it."""

    def __init__(self, step: str) -> None:
        super().__init__(f"tmux activation ran out of time during {step}")
        self.step = step


class ActivationBudget:
    """One deadline for a whole activation, drawn down step by step.

    Every awaited step is capped at the smaller of its own timeout and what is
    left, so no sequence of slow steps can sum past the deadline.
    """

    def __init__(self, seconds: float) -> None:
        self._loop = asyncio.get_running_loop()
        self._deadline = self._loop.time() + seconds
        self._step = "start"

    def remaining(self) -> float:
        """Return the seconds left, which may be zero or negative."""
        return self._deadline - self._loop.time()

    def slice(self, step: str, cap: float) -> float:
        """Reserve at most ``cap`` seconds for ``step``, or raise if none are left."""
        remaining = self.remaining()
        if remaining <= 0:
            raise ActivationBudgetExpired(step)
        self._step = step
        return min(cap, remaining)

    async def run[T](self, step: str, cap: float, awaitable: Awaitable[T]) -> T:
        """Await one step within its share of the budget.

        Callers build the awaitable at the call site, so an already-expired
        budget has to dispose of a coroutine that will never be awaited.
        """
        try:
            timeout = self.slice(step, cap)
        except ActivationBudgetExpired:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise
        try:
            return await asyncio.wait_for(awaitable, timeout)
        except TimeoutError:
            raise ActivationBudgetExpired(step) from None

    def check(self) -> None:
        """Raise if the step that just finished used up the budget."""
        if self.remaining() <= 0:
            raise ActivationBudgetExpired(self._step)


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


def parse_dimension(value: Any, limit: int) -> int | None:
    """Return an in-bounds terminal dimension, or ``None`` if unusable.

    These arrive from an untrusted client and they size the tmux pane, so the
    wire type is checked rather than coerced: ``int()`` would quietly accept
    ``True`` as 1, ``24.9`` as 24, and ``"200"`` as 200. The web client sends
    floored integers, so nothing legitimate is rejected here.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if 0 < value <= limit else None


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

    The whole sequence runs under one deadline; running out of it is a typed
    failure like any other, so the client is told rather than left waiting on a
    request that has already been abandoned.
    """
    budget = ActivationBudget(ACTIVATION_BUDGET_SECONDS)
    try:
        await _activate(host, websocket, streaming_id, pending, rows, cols, budget)
    except ActivationBudgetExpired as exc:
        logger.warning(
            "tmux activation for '%s' exceeded its budget: %s", pending.session_name, exc
        )
        # The client renders this after a full stop, so it stands alone as a
        # sentence; which step ran out is a daemon-log concern.
        await _fail(
            host, websocket, streaming_id, "activation_timed_out", "It took too long to start."
        )


async def _activate(
    host: TmuxAttachHost,
    websocket: Any,
    streaming_id: str,
    pending: PendingAttachment,
    rows: int,
    cols: int,
    budget: ActivationBudget,
) -> None:
    """Run the activation steps, each bounded by its share of ``budget``.

    Every awaited boundary revalidates: connections are separate tasks, so a
    different websocket can kill the session, reap the bridge, or stop the
    server mid-activation.
    """
    session_name = pending.session_name
    socket = pending.socket
    manager = host._get_tmux_manager(socket)
    config = host._get_tmux_config(socket)

    await budget.run(
        "reap",
        TMUX_STEP_TIMEOUT_SECONDS,
        reap_stale_attachments(
            host, session_name, socket, websocket, exclude_streaming_id=streaming_id
        ),
    )
    if not _still_activating(host, streaming_id, pending):
        return
    budget.check()

    # Hide the status bar and enable mouse reporting before the tmux client's
    # first paint, so it never renders chrome the web terminal has no use for.
    try:
        await budget.run(
            "set-option status",
            TMUX_STEP_TIMEOUT_SECONDS,
            manager.set_option(session_name, "status", "off"),
        )
        await budget.run(
            "set-option mouse",
            TMUX_STEP_TIMEOUT_SECONDS,
            manager.set_option(session_name, "mouse", "on"),
        )
    except ActivationBudgetExpired:
        raise
    except Exception as exc:
        logger.debug("Failed to configure tmux session '%s': %s", session_name, exc)
    if not _still_activating(host, streaming_id, pending):
        return
    budget.check()

    try:
        master_fd = await budget.run(
            "bridge attach",
            TMUX_STEP_TIMEOUT_SECONDS,
            host._tmux_bridge.attach(
                session_name=session_name,
                streaming_id=streaming_id,
                config=config,
                rows=rows,
                cols=cols,
            ),
        )
    except ActivationBudgetExpired:
        raise
    except Exception as exc:
        logger.error("Failed to build tmux bridge for '%s': %s", session_name, exc)
        await _fail(host, websocket, streaming_id, "bridge_failed", f"Attach failed: {exc}")
        return

    # Register before the next await: a teardown landing during capture must
    # find a bridge to clean up.
    host._tmux_client_bridges.setdefault(websocket, set()).add(streaming_id)

    bridge = await budget.run(
        "bridge lookup", TMUX_STEP_TIMEOUT_SECONDS, host._tmux_bridge.get_bridge(streaming_id)
    )
    if bridge is None:
        await _fail(
            host, websocket, streaming_id, "bridge_failed", "Bridge disappeared after attach"
        )
        return
    if not _still_activating(host, streaming_id, pending):
        await _teardown(host, streaming_id)
        return
    budget.check()

    client_tty = await _wait_for_client(
        manager,
        session_name,
        bridge.proc.pid,
        timeout=budget.slice("client registration", CLIENT_REGISTRATION_TIMEOUT_SECONDS),
    )
    if not client_tty:
        if not _still_activating(host, streaming_id, pending):
            await _teardown(host, streaming_id)
            return
        # A poll cut short because its slice ran out says nothing about tmux;
        # it is the deadline that expired, and the code has to say so.
        budget.check()
        await _fail(
            host,
            websocket,
            streaming_id,
            "client_registration_failed",
            f"tmux client for '{session_name}' never registered",
        )
        return
    if not await _checkpoint(host, websocket, streaming_id, pending, bridge.proc, budget):
        return

    # The capture also repaints our own client, in the same tmux command list.
    # The redraw is what the web terminal actually renders as its screen -- it
    # clears first, which discards the stale paint tmux made at attach and is
    # why the seam has no duplicate row. Deciding the two together is what
    # keeps it from having a gap either: issued separately, the redraw lands a
    # round trip after the capture, and whatever scrolled out of the pane in
    # between is in neither half.
    history = HistoryCapture(text="", truncated=False, dropped_bytes=0, total_bytes=0)
    # Capture draws from the budget minus the tail the delivery steps need. A
    # capture that would leave nothing to deliver with is not attempted at all.
    capture_timeout = min(
        CAPTURE_TIMEOUT_SECONDS, budget.remaining() - ACTIVATION_TAIL_RESERVE_SECONDS
    )
    unavailable = capture_timeout <= 0
    if config.attach_history_lines <= 0:
        # Configured off: the empty frame is the intended payload, not a
        # degraded one, so it carries unavailable=False and renders no marker.
        unavailable = False
    elif unavailable:
        logger.warning(
            "Skipping tmux history capture for '%s': too little of the activation budget left",
            session_name,
        )
    else:
        try:
            history = await capture_history(
                manager,
                session_name,
                max_lines=config.attach_history_lines,
                timeout=capture_timeout,
                refresh_tty=client_tty,
            )
        except HistoryCaptureError as exc:
            logger.warning("tmux history capture failed for '%s': %s", session_name, exc)
            unavailable = True

    if not await _checkpoint(host, websocket, streaming_id, pending, bridge.proc, budget):
        return
    if unavailable:
        alive = await budget.run(
            "session probe", TMUX_STEP_TIMEOUT_SECONDS, manager.has_session(session_name)
        )
        if not alive:
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
        # The probe is itself an await: a cross-socket kill can land while it
        # is suspended, and the answer it returns is about the moment before.
        if not await _checkpoint(host, websocket, streaming_id, pending, bridge.proc, budget):
            return

    frame = json_dumps(
        {
            "type": "terminal_attach_history",
            "streaming_id": streaming_id,
            "text": history.text,
            "truncated": history.truncated,
            "unavailable": unavailable,
            "dropped_bytes": history.dropped_bytes,
            "total_bytes": history.total_bytes,
        },
        # json_dumps defaults to ensure_ascii=True, which renders every ESC
        # byte and every box-drawing glyph as a six-byte \uXXXX escape. Agent
        # CLIs are full of both.
        ensure_ascii=False,
    )
    try:
        await budget.run("history send", TMUX_STEP_TIMEOUT_SECONDS, websocket.send(frame))
    except ActivationBudgetExpired:
        raise
    except Exception as exc:
        # The socket is broken, so a second frame on it cannot be trusted to
        # arrive; the client learns through the connection-close path.
        logger.debug("Failed to send tmux history for %s: %s", streaming_id, exc)
        host._tmux_pending.pop(streaming_id, None)
        await _teardown(host, streaming_id)
        return

    if not await _checkpoint(host, websocket, streaming_id, pending, bridge.proc, budget):
        return

    from gobby.agents.pty_reader import get_pty_reader_manager
    from gobby.agents.tmux.alt_screen import AltScreenFilter

    reader = get_pty_reader_manager()
    # tmux opens its stream with smcup, which parks the client on the
    # alternate screen for the whole attachment. The history written just
    # above lives in the primary screen's scrollback, which the alternate
    # screen has none of -- so without this filter the restored window is
    # retained by the VT and unreachable until detach.
    started = await budget.run(
        "reader start",
        TMUX_STEP_TIMEOUT_SECONDS,
        reader.start_reader(
            cast(Any, _BridgeAgent(streaming_id, master_fd)),
            transform=AltScreenFilter(),
        ),
    )
    if not started:
        await _fail(
            host,
            websocket,
            streaming_id,
            "reader_failed",
            f"PTY reader refused to start for '{session_name}'",
        )
        return
    if not await _checkpoint(host, websocket, streaming_id, pending, bridge.proc, budget):
        return

    if unavailable or not history.repainted:
        # A capture repaints alongside itself, and that is the repaint the
        # seam depends on. What is left here is an attachment that has no
        # history to be consistent with, or one whose repaint failed -- both
        # still need a screen. The repaint is a convenience the stream
        # recovers from on its own, so a budget that runs out here must not
        # undo an otherwise working attachment.
        try:
            await manager.refresh_client(
                session_name, timeout=min(TMUX_STEP_TIMEOUT_SECONDS, max(budget.remaining(), 0.0))
            )
        except Exception as exc:
            logger.debug("Post-activation refresh-client failed: %s", exc)

    # The reader is the last await before finalizing, and finalizing means
    # dropping the reservation that teardown keys on -- so a bridge that died
    # during it would be left registered and presented as a live stream.
    if not _still_activating(host, streaming_id, pending):
        await _teardown(host, streaming_id)
        return
    if bridge.proc.returncode is not None:
        await _fail(
            host,
            websocket,
            streaming_id,
            "bridge_exited",
            f"tmux client exited during attach (rc={bridge.proc.returncode})",
        )
        return

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
    budget: ActivationBudget,
) -> bool:
    """Revalidate after an awaited step; clean up and return False on loss.

    Raises:
        ActivationBudgetExpired: the step that just finished used up the
            deadline, so there is no time left to build the rest.
    """
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
    budget.check()
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
) -> str | None:
    """Poll ``list-clients`` until tmux reports our attach process as a client.

    Returns that client's tty, which is what ``refresh-client`` targets, or
    ``None`` if it never registered. A registered client always has a tty, so
    the tty doubles as the registration signal.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    target = str(pid)
    while True:
        tty = (await _list_client_ttys(manager, session_name)).get(target)
        if tty:
            return tty
        if loop.time() >= deadline:
            logger.debug("tmux client pid %s never appeared for session '%s'", target, session_name)
            return None
        await asyncio.sleep(CLIENT_REGISTRATION_POLL_SECONDS)


async def _list_client_ttys(manager: TmuxSessionManager, session_name: str) -> dict[str, str]:
    """Map client pid to tty for a session, empty on any failure."""
    args = [
        *manager.base_args(),
        "list-clients",
        "-t",
        _exact_session_target(session_name),
        "-F",
        "#{client_pid} #{client_tty}",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.debug("tmux list-clients could not start for '%s': %s", session_name, exc)
        return {}

    try:
        stdout_bytes, _stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=LIST_CLIENTS_TIMEOUT_SECONDS
        )
    except TimeoutError:
        await kill_and_reap(proc)
        return {}
    except asyncio.CancelledError:
        await kill_and_reap(proc)
        raise

    if proc.returncode:
        return {}
    decoded = (stdout_bytes or b"").decode("utf-8", errors="replace")
    ttys: dict[str, str] = {}
    for line in decoded.splitlines():
        client_pid, _, client_tty = line.strip().partition(" ")
        if client_pid and client_tty:
            ttys[client_pid] = client_tty.strip()
    return ttys


def _owner_is_closed(owner: Any) -> bool:
    """Return whether a websocket owner is already gone."""
    closed = getattr(owner, "closed", None)
    if isinstance(closed, bool):
        return closed
    return getattr(owner, "close_code", None) is not None
