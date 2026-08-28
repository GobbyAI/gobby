"""Reserve-then-activate state machine for tmux web attachments.

``terminal_attach`` on a ``backend == "tmux"`` row reserves the attachment id
and acknowledges it without touching tmux. The client's first
``terminal_resize`` activates the reservation: that is the earliest point at
which the real terminal geometry is known, so a real ``tmux attach-session``
client is spawned exactly once at the right size in its own PTY, history is
captured at that width, and the PTY reader starts only after the history
frame is on the wire. The pane reflows to the browser while the web is
attached, which is the tmux-client contract; the gterm host proxy remains the
delivery for native rows.

Activation is awaited inline by the resize handler. ``WebSocketServer`` handles
one message at a time per connection, so awaiting here keeps this socket's
``terminal_input`` and ``terminal_detach`` queued behind activation instead of
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
from gobby.agents.tmux.session_activation import exact_session_target
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.utils.json_helpers import json_dumps

logger = logging.getLogger(__name__)

# ``TmuxPTYBridge.attach`` returns once the subprocess exists, which is before
# tmux has necessarily registered the client and applied the pane geometry --
# capturing earlier can read the previous pane width.
CLIENT_REGISTRATION_TIMEOUT_SECONDS = 2.0
CLIENT_REGISTRATION_POLL_SECONDS = 0.05
LIST_CLIENTS_TIMEOUT_SECONDS = 2.0

# One deadline for the whole activation. Activation is awaited inline and the
# connection dispatches one message at a time, so everything the user does next
# queues behind it -- and the web client abandons a terminal request after 10s,
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

    terminal_id: str
    session_name: str
    manager: TmuxSessionManager
    config: TmuxConfig
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

    def _leases(self) -> TerminalLeaseRegistry: ...


def cancel_pending(host: TmuxAttachHost, attachment_id: str) -> None:
    """Drop a reservation and signal any in-flight activation to abort."""
    pending = host._tmux_pending.pop(attachment_id, None)
    if pending is not None:
        pending.cancelled = True


def cancel_pending_for_owner(host: TmuxAttachHost, websocket: Any) -> None:
    """Drop every reservation held by a disconnecting client."""
    for attachment_id, pending in list(host._tmux_pending.items()):
        if pending.owner is websocket:
            cancel_pending(host, attachment_id)


def cancel_pending_for_terminal(host: TmuxAttachHost, terminal_id: str) -> None:
    """Drop every reservation targeting a terminal that is going away."""
    for attachment_id, pending in list(host._tmux_pending.items()):
        if pending.terminal_id == terminal_id:
            cancel_pending(host, attachment_id)


def cancel_all_pending(host: TmuxAttachHost) -> None:
    """Drop every reservation, for server shutdown."""
    for attachment_id in list(host._tmux_pending):
        cancel_pending(host, attachment_id)


def cancel_stale_reservations(
    host: TmuxAttachHost,
    terminal_id: str,
    websocket: Any,
    *,
    exclude_attachment_id: str | None = None,
) -> None:
    """Drop this client's (or a dead client's) reservations for one terminal.

    A reservation left behind by a duplicate attach has no bridge, so a later
    resize naming that older id would happily build a second tmux client past
    the reap. A reservation held by a different, still-live socket is a second
    viewer-to-be and is left alone.
    """
    for attachment_id, pending in list(host._tmux_pending.items()):
        if attachment_id == exclude_attachment_id:
            continue
        if pending.terminal_id != terminal_id:
            continue
        if pending.owner is not websocket and not _owner_is_closed(pending.owner):
            continue
        cancel_pending(host, attachment_id)
        logger.debug(
            "Cancelled stale tmux reservation %s for terminal %s", attachment_id, terminal_id
        )


async def reap_stale_attachments(
    host: TmuxAttachHost,
    terminal_id: str,
    websocket: Any,
    *,
    exclude_attachment_id: str | None = None,
) -> None:
    """Tear down this client's stale bridges and reservations for one terminal.

    Both the reader key and the bridge key are the attachment id, so a
    re-attach otherwise adds a *second* ``tmux attach-session`` client to the
    same session. tmux then sizes the session against both clients and the
    capture width stops matching the repaint width, which is what makes
    restored history wrap wrong.
    """
    owner_of: dict[str, Any] = {}
    for owner, bridge_ids in host._tmux_client_bridges.items():
        for bridge_id in bridge_ids:
            owner_of[bridge_id] = owner

    for attachment_id, bridge in list((await host._tmux_bridge.list_bridges()).items()):
        if attachment_id == exclude_attachment_id:
            continue
        if bridge.terminal_id != terminal_id:
            continue
        owner = owner_of.get(attachment_id)
        # A bridge held by a different, still-live socket is a second viewer.
        # An unowned bridge is an orphan its owner's cleanup already forgot.
        if owner is not None and owner is not websocket and not _owner_is_closed(owner):
            continue
        await teardown_bridge(host, attachment_id)
        logger.debug("Reaped stale tmux bridge %s for terminal %s", attachment_id, terminal_id)

    cancel_stale_reservations(
        host,
        terminal_id,
        websocket,
        exclude_attachment_id=exclude_attachment_id,
    )


async def teardown_bridge(host: TmuxAttachHost, attachment_id: str) -> None:
    """Stop the reader, detach the bridge, and forget the reservation and owner."""
    from gobby.agents.pty_reader import get_pty_reader_manager

    cancel_pending(host, attachment_id)
    reader = get_pty_reader_manager()
    await reader.stop_reader(attachment_id)
    await host._tmux_bridge.detach(attachment_id)
    for bridge_ids in host._tmux_client_bridges.values():
        bridge_ids.discard(attachment_id)


async def teardown_terminal_bridges(host: TmuxAttachHost, terminal_id: str) -> None:
    """Tear down every viewer of a terminal that is being killed."""
    cancel_pending_for_terminal(host, terminal_id)
    for attachment_id, bridge in list((await host._tmux_bridge.list_bridges()).items()):
        if bridge.terminal_id == terminal_id:
            await teardown_bridge(host, attachment_id)


async def activate_attachment(
    host: TmuxAttachHost,
    websocket: Any,
    attachment_id: str,
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
        await _activate(host, websocket, attachment_id, pending, rows, cols, budget)
    except ActivationBudgetExpired as exc:
        logger.warning(
            "tmux activation for '%s' exceeded its budget: %s", pending.session_name, exc
        )
        await _fail(host, websocket, attachment_id, pending, "activation_timed_out")


async def _activate(
    host: TmuxAttachHost,
    websocket: Any,
    attachment_id: str,
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
    manager = pending.manager
    config = pending.config

    await budget.run(
        "reap",
        TMUX_STEP_TIMEOUT_SECONDS,
        reap_stale_attachments(
            host, pending.terminal_id, websocket, exclude_attachment_id=attachment_id
        ),
    )
    if not _still_activating(host, attachment_id, pending):
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
    if not _still_activating(host, attachment_id, pending):
        return
    budget.check()

    try:
        master_fd = await budget.run(
            "bridge attach",
            TMUX_STEP_TIMEOUT_SECONDS,
            host._tmux_bridge.attach(
                session_name=session_name,
                streaming_id=attachment_id,
                config=config,
                rows=rows,
                cols=cols,
                terminal_id=pending.terminal_id,
            ),
        )
    except ActivationBudgetExpired:
        raise
    except Exception as exc:
        logger.error("Failed to build tmux bridge for '%s': %s", session_name, exc)
        await _fail(host, websocket, attachment_id, pending, "bridge_failed")
        return

    # Register before the next await: a teardown landing during capture must
    # find a bridge to clean up.
    host._tmux_client_bridges.setdefault(websocket, set()).add(attachment_id)

    bridge = await budget.run(
        "bridge lookup", TMUX_STEP_TIMEOUT_SECONDS, host._tmux_bridge.get_bridge(attachment_id)
    )
    if bridge is None:
        await _fail(host, websocket, attachment_id, pending, "bridge_failed")
        return
    if not _still_activating(host, attachment_id, pending):
        await teardown_bridge(host, attachment_id)
        return
    budget.check()

    client_tty = await _wait_for_client(
        manager,
        session_name,
        bridge.proc.pid,
        timeout=budget.slice("client registration", CLIENT_REGISTRATION_TIMEOUT_SECONDS),
    )
    if not client_tty:
        if not _still_activating(host, attachment_id, pending):
            await teardown_bridge(host, attachment_id)
            return
        # A poll cut short because its slice ran out says nothing about tmux;
        # it is the deadline that expired, and the code has to say so.
        budget.check()
        logger.warning("tmux client for '%s' never registered", session_name)
        await _fail(host, websocket, attachment_id, pending, "client_registration_failed")
        return
    if not await _checkpoint(host, websocket, attachment_id, pending, bridge.proc, budget):
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

    if not await _checkpoint(host, websocket, attachment_id, pending, bridge.proc, budget):
        return
    if unavailable:
        alive = await budget.run(
            "session probe", TMUX_STEP_TIMEOUT_SECONDS, manager.has_session(session_name)
        )
        if not alive:
            # Nothing is left to stream, so degrading would hand the user a
            # terminal that can never produce a byte.
            logger.warning("Session '%s' disappeared during attach", session_name)
            await _fail(host, websocket, attachment_id, pending, "session_missing")
            return
        # The probe is itself an await: a cross-socket kill can land while it
        # is suspended, and the answer it returns is about the moment before.
        if not await _checkpoint(host, websocket, attachment_id, pending, bridge.proc, budget):
            return

    frame = json_dumps(
        {
            "type": "terminal_attach_history",
            "terminal_id": pending.terminal_id,
            "attachment_id": attachment_id,
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
        logger.debug("Failed to send tmux history for %s: %s", attachment_id, exc)
        await teardown_bridge(host, attachment_id)
        return

    if not await _checkpoint(host, websocket, attachment_id, pending, bridge.proc, budget):
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
            cast(Any, _BridgeAgent(attachment_id, master_fd)),
            transform=AltScreenFilter(),
        ),
    )
    if not started:
        logger.warning("PTY reader refused to start for '%s'", session_name)
        await _fail(host, websocket, attachment_id, pending, "reader_failed")
        return
    if not await _checkpoint(host, websocket, attachment_id, pending, bridge.proc, budget):
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
    if not _still_activating(host, attachment_id, pending):
        await teardown_bridge(host, attachment_id)
        return
    if bridge.proc.returncode is not None:
        logger.warning(
            "tmux client exited during attach (rc=%s) for '%s'",
            bridge.proc.returncode,
            session_name,
        )
        await _fail(host, websocket, attachment_id, pending, "bridge_exited")
        return

    if host._tmux_pending.get(attachment_id) is pending:
        del host._tmux_pending[attachment_id]


def _still_activating(host: TmuxAttachHost, attachment_id: str, pending: PendingAttachment) -> bool:
    """Return whether this activation still owns the reservation."""
    return host._tmux_pending.get(attachment_id) is pending and not pending.cancelled


async def _checkpoint(
    host: TmuxAttachHost,
    websocket: Any,
    attachment_id: str,
    pending: PendingAttachment,
    proc: asyncio.subprocess.Process,
    budget: ActivationBudget,
) -> bool:
    """Revalidate after an awaited step; clean up and return False on loss.

    Raises:
        ActivationBudgetExpired: the step that just finished used up the
            deadline, so there is no time left to build the rest.
    """
    if not _still_activating(host, attachment_id, pending):
        await teardown_bridge(host, attachment_id)
        return False
    if proc.returncode is not None:
        logger.warning(
            "tmux client exited during attach (rc=%s) for '%s'",
            proc.returncode,
            pending.session_name,
        )
        await _fail(host, websocket, attachment_id, pending, "bridge_exited")
        return False
    budget.check()
    return True


async def _fail(
    host: TmuxAttachHost,
    websocket: Any,
    attachment_id: str,
    pending: PendingAttachment,
    reason: str,
) -> None:
    """Finalize a failed activation and tell the client its attachment ended.

    ``terminal_attachment_finalized`` is the frame the web already retires a
    stream on, and it is matched by attachment id rather than against a
    pending request -- ``terminal_attach_result`` cleared that entry by
    activation time, so a post-ack ``error`` frame would be silently swallowed
    and the UI would stay attached to a dead stream.
    """
    await teardown_bridge(host, attachment_id)
    event = host._leases().finalize(attachment_id, reason)
    generation = (
        event.lease_generation
        if event is not None
        else host._leases().generation(pending.terminal_id)
    )
    try:
        await websocket.send(
            json_dumps(
                {
                    "type": "terminal_attachment_finalized",
                    "terminal_id": pending.terminal_id,
                    "attachment_id": attachment_id,
                    "reason": reason,
                    "lease_generation": generation,
                }
            )
        )
    except Exception as exc:
        logger.debug("Failed to deliver activation failure for %s: %s", attachment_id, exc)


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
        exact_session_target(session_name),
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
