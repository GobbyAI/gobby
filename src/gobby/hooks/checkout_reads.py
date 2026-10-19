"""Hub-side checkout reads for node-origin hook deliveries (#24522).

A node-origin delivery reads its checkout from the node that owns it, through the
hub-mode `checkout.read` command. Local deliveries read as they always did. File
bytes and git output live only in the delivery's memo: they never reach hook
events, logs, the database, session variables or the broadcaster.
"""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
from collections.abc import Awaitable, Coroutine, Iterator, Sequence
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from gobby.hub.node_client import NodeCommandError, node_command_client
from gobby.utils import spawn
from gobby.utils.machine_id import get_machine_id

_COMMAND = "checkout.read"
_FILES_TIMEOUT_MS = 5000
# A per-request answer names a bad query or scope; the node is healthy, so it does not latch.
_PER_REQUEST_CODES = frozenset({"invalid_payload", "no_project_root", "outside"})
# Slack past a request's own bound, so a stuck bridge still returns to the caller.
_BRIDGE_MARGIN_S = 10.0


class CheckoutReadUnavailable(OSError):
    """A node checkout read went unanswered; `code` says why and `machine_id` names the node."""

    def __init__(self, code: str, machine_id: str) -> None:
        self.code = code
        self.machine_id = machine_id
        super().__init__(f"checkout_read_unavailable: {code} (machine {machine_id})")


@dataclass(frozen=True)
class PathFact:
    path: str
    resolved: str
    kind: Literal["file", "dir", "other", "missing", "outside"]
    checkout_root: str | None
    git_common_dir: str | None
    project_root: str | None
    project_id: str | None
    data: bytes | None
    truncated: bool


@dataclass(frozen=True)
class CheckoutFacts:
    root: str
    project_id: str
    home: str
    temp_dir: str
    files: tuple[PathFact, ...]


@dataclass
class _NodeDelivery:
    """One node-origin delivery: its machine, the daemon loop, and the content memo."""

    machine: str
    loop: asyncio.AbstractEventLoop
    cwd: str
    header: CheckoutFacts | None = None
    files: dict[tuple[str, int | None, str], PathFact] = field(default_factory=dict)
    latched: str | None = None


_DELIVERY: ContextVar[_NodeDelivery | None] = ContextVar("checkout_read_delivery", default=None)


@contextmanager
def delivery_scope(
    machine_id: str | None, loop: asyncio.AbstractEventLoop, cwd: str
) -> Iterator[None]:
    """Bind one delivery's origin. A missing or hub machine id is local."""
    delivery = (
        _NodeDelivery(machine_id, loop, cwd)
        if machine_id and machine_id != get_machine_id()
        else None
    )
    token = _DELIVERY.set(delivery)
    try:
        yield
    finally:
        _DELIVERY.reset(token)


def node_origin() -> bool:
    return _DELIVERY.get() is not None


def event_machine_id() -> str | None:
    delivery = _DELIVERY.get()
    return get_machine_id() if delivery is None else delivery.machine


def path_facts(
    paths: Sequence[str], *, cwd: str | None = None, read_limit: int | None = None
) -> CheckoutFacts:
    """Facts for `paths` on the delivery's node. Only paths the memo lacks are queried."""
    delivery = _require_node_delivery()
    target = delivery.cwd if cwd is None else cwd
    missing = list(dict.fromkeys(p for p in paths if (target, read_limit, p) not in delivery.files))
    header = delivery.header
    if missing or header is None:
        header = _read_files(delivery, target, read_limit, missing)
    return replace(header, files=tuple(delivery.files[(target, read_limit, p)] for p in paths))


def run_git(
    args: Sequence[str], *, cwd: str, timeout: float, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run git in `cwd` here, or on the delivery's node for a node-origin delivery."""
    delivery = _DELIVERY.get()
    if delivery is None:
        return spawn.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            timeout=timeout,
            input=input_bytes,
            env=_local_git_env(),
        )
    reply = _send(delivery, _git_payload(cwd, args, timeout, input_bytes), _timeout_ms(timeout))
    return _git_result(delivery, args, timeout, reply["git"])


async def run_git_async(
    args: Sequence[str], *, cwd: str, timeout: float, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Awaitable form of `run_git`, for callers on the daemon loop."""
    delivery = _DELIVERY.get()
    if delivery is None:
        return await asyncio.to_thread(
            run_git, args, cwd=cwd, timeout=timeout, input_bytes=input_bytes
        )
    payload = _git_payload(cwd, args, timeout, input_bytes)
    reply = await _send_async(delivery, payload, _timeout_ms(timeout))
    return _git_result(delivery, args, timeout, reply["git"])


def _require_node_delivery() -> _NodeDelivery:
    delivery = _DELIVERY.get()
    if delivery is None:
        raise RuntimeError("path_facts is only called on node-origin branches")
    return delivery


def _read_files(
    delivery: _NodeDelivery, cwd: str, read_limit: int | None, paths: list[str]
) -> CheckoutFacts:
    query = {"kind": "files", "paths": paths, "read_limit": read_limit}
    reply = _send(delivery, {"cwd": cwd, "query": query}, _FILES_TIMEOUT_MS)
    for entry in reply["files"]:
        delivery.files[(cwd, read_limit, entry["path"])] = _path_fact(entry)
    header = CheckoutFacts(
        root=reply["root"],
        project_id=reply["project_id"],
        home=reply["home"],
        temp_dir=reply["temp_dir"],
        files=(),
    )
    delivery.header = header
    return header


def _path_fact(entry: dict[str, Any]) -> PathFact:
    data = entry["data"]
    return PathFact(
        path=entry["path"],
        resolved=entry["resolved"],
        kind=entry["kind"],
        checkout_root=entry["checkout_root"],
        git_common_dir=entry["git_common_dir"],
        project_root=entry["project_root"],
        project_id=entry["project_id"],
        data=None if data is None else base64.b64decode(data),
        truncated=entry["truncated"],
    )


def _git_payload(
    cwd: str, args: Sequence[str], timeout: float, input_bytes: bytes | None
) -> dict[str, Any]:
    stdin = None if input_bytes is None else base64.b64encode(input_bytes).decode("ascii")
    query = {
        "kind": "git",
        "args": list(args),
        "stdin": stdin,
        "timeout_ms": _timeout_ms(timeout),
    }
    return {"cwd": cwd, "query": query}


def _git_result(
    delivery: _NodeDelivery, args: Sequence[str], timeout: float, git: dict[str, Any]
) -> subprocess.CompletedProcess[bytes]:
    if git["timed_out"]:
        raise subprocess.TimeoutExpired(["git", *args], timeout)
    if git["truncated"]:
        raise CheckoutReadUnavailable("truncated", delivery.machine)
    if git["exit_code"] is None:
        raise _unavailable(delivery, "node_reply_lost")
    return subprocess.CompletedProcess(
        ["git", *args],
        git["exit_code"],
        stdout=base64.b64decode(git["stdout"]),
        stderr=git["stderr"].encode("utf-8"),
    )


def _local_git_env() -> dict[str, str]:
    """The inherited environment without GIT_* selectors, so `git -C cwd` reads cwd's repo."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _submit(
    delivery: _NodeDelivery, call: Coroutine[Any, Any, dict[str, Any]]
) -> Future[dict[str, Any]]:
    """Hand `call` to the daemon loop. A closed loop latches `channel_closed` and closes `call`."""
    try:
        return asyncio.run_coroutine_threadsafe(call, delivery.loop)
    except RuntimeError as exc:
        call.close()
        raise _unavailable(delivery, "channel_closed") from exc


def _send(delivery: _NodeDelivery, payload: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    """Send one checkout.read from a worker thread and wait for the daemon loop to answer."""
    _check_latch(delivery)
    if _running_loop() is delivery.loop:
        # Blocking here would wait on the loop this thread runs; fail instead of deadlocking.
        raise _unavailable(delivery, "loop_thread")
    call = node_command_client().send(delivery.machine, _COMMAND, payload, timeout_ms)
    future = _submit(delivery, call)
    try:
        return future.result(timeout=timeout_ms / 1000 + _BRIDGE_MARGIN_S)
    except NodeCommandError as exc:
        raise _unavailable(delivery, exc.code) from exc
    except TimeoutError as exc:
        future.cancel()
        raise _unavailable(delivery, "node_timeout") from exc


async def _send_async(
    delivery: _NodeDelivery, payload: dict[str, Any], timeout_ms: int
) -> dict[str, Any]:
    _check_latch(delivery)
    call = node_command_client().send(delivery.machine, _COMMAND, payload, timeout_ms)
    if _running_loop() is delivery.loop:
        pending: Awaitable[dict[str, Any]] = call
    else:
        pending = asyncio.wrap_future(_submit(delivery, call))
    try:
        return await asyncio.wait_for(pending, timeout=timeout_ms / 1000 + _BRIDGE_MARGIN_S)
    except NodeCommandError as exc:
        raise _unavailable(delivery, exc.code) from exc
    except TimeoutError as exc:
        raise _unavailable(delivery, "node_timeout") from exc


def _check_latch(delivery: _NodeDelivery) -> None:
    if delivery.latched is not None:
        raise CheckoutReadUnavailable(delivery.latched, delivery.machine)


def _unavailable(delivery: _NodeDelivery, code: str) -> CheckoutReadUnavailable:
    if code not in _PER_REQUEST_CODES:
        delivery.latched = code
    return CheckoutReadUnavailable(code, delivery.machine)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _timeout_ms(timeout: float) -> int:
    return int(timeout * 1000)
