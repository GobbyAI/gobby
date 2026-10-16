"""Python's half of native WS publication: bind to gdaemon and publish broadcasts in order."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from collections import deque
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from gobby.runner_backend import supervisor_write_token
from gobby.utils.json_helpers import json_dumps

if TYPE_CHECKING:
    from gobby.config.bootstrap import BootstrapConfig
    from gobby.servers.websocket.models import WebSocketConfig

logger = logging.getLogger(__name__)

# Decision 14 admission budgets, counting queued plus in-flight operations.
MAX_RETAINED_BYTES = 128 * 1024 * 1024
MAX_RETAINED_OPS = 65_536
MAX_WAITING_FUTURES = 65_536

REQUEST_TIMEOUT = 2.0
TOKEN_WAIT = 2.0
TOKEN_POLL_INTERVAL = 0.05
REBIND_INTERVAL = 5.0
LEG_CLOSE_TIMEOUT = 1.0

BIND_PATH = "/_gobby/ws/bind"
PUBLISH_PATH = "/_gobby/ws/publish"


class NativeLeg(Protocol):
    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class _BindFailed(Exception):
    pass


@dataclass(slots=True)
class _Op:
    generation: str
    encoded: bytes
    future: asyncio.Future[None]
    settled: bool = False


class NativePublisher:
    """One ordered operation queue to gdaemon, drained by one sender with one request in flight.

    Time and budget constants are read from the module globals at call time.
    """

    def __init__(
        self,
        *,
        secret: str,
        base_url: str,
        config: WebSocketConfig,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._secret = secret
        self._base_url = base_url.rstrip("/")
        self._config = config
        self._client = httpx.AsyncClient(transport=transport)
        self._generation: str | None = None
        self._legs: dict[str, set[NativeLeg]] = {}
        self._queue: deque[_Op] = deque()
        self._retained_bytes = 0
        self._retained_ops = 0
        self._in_flight: tuple[list[_Op], asyncio.Task[bool]] | None = None
        self._wake = asyncio.Event()
        self._sender: asyncio.Task[None] | None = None
        self._rebind: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[None]] = set()
        self._stopped = False
        self._closed = False

    @property
    def front_door_secret(self) -> str:
        return self._secret

    @property
    def generation(self) -> str | None:
        """The current bound, non-failed generation."""
        return self._generation

    @property
    def retained_bytes(self) -> int:
        return self._retained_bytes

    @property
    def retained_ops(self) -> int:
        return self._retained_ops

    def register_leg(self, generation: str, leg: NativeLeg) -> bool:
        if self._stopped or generation != self._generation:
            return False
        self._legs.setdefault(generation, set()).add(leg)
        return True

    def unregister_leg(self, generation: str, leg: NativeLeg) -> None:
        self._legs.get(generation, set()).discard(leg)

    async def start(self) -> None:
        """Bind once; a failure enters the rebind loop instead of raising."""
        try:
            await self.bind()
        except (_BindFailed, httpx.HTTPError, TimeoutError) as exc:
            logger.warning("Native WS bind failed; retrying every %ss: %s", REBIND_INTERVAL, exc)
            self._schedule_rebind(immediate=False)

    async def bind(self) -> str:
        token = await self._await_token()
        body = json_dumps(
            {
                "max_message_size": self._config.max_message_size,
                "ping_interval": self._config.ping_interval,
                "ping_timeout": self._config.ping_timeout,
            }
        ).encode()
        response = await asyncio.wait_for(
            self._client.post(
                f"{self._base_url}{BIND_PATH}", content=body, headers=self._headers(token)
            ),
            REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            raise _BindFailed(f"bind answered {response.status_code}")
        try:
            generation = response.json()["generation"]
        except (ValueError, KeyError, TypeError) as exc:
            raise _BindFailed("bind answered without a generation") from exc
        if not isinstance(generation, str) or not generation:
            raise _BindFailed("bind answered without a generation")
        if self._stopped:
            raise _BindFailed("publisher stopped during bind")
        if self._generation is not None:
            self._retire(self._generation)
        self._generation = generation
        return generation

    def publish(
        self, message: dict[str, Any], frames: Sequence[dict[str, Any]] = ()
    ) -> asyncio.Future[None]:
        # Frames are encoded exactly as BroadcastMixin.broadcast encodes them.
        payloads = [json_dumps(frame) for frame in frames] or [json_dumps(message)]
        return self._enqueue({"op": "publish", "filter": message, "frames": payloads})

    def subscribe(self, leg: int, events: Iterable[str]) -> asyncio.Future[None]:
        return self._enqueue({"op": "subscribe", "leg": leg, "events": list(events)})

    def fail_generation(self, generation: str | None) -> None:
        """Retire the current generation without retrying its work, then rebind."""
        if generation is None or generation != self._generation:
            return
        self._retire(generation)
        self._schedule_rebind(immediate=True)

    async def stop(self) -> None:
        self._stopped = True
        self._generation = None
        self._drop_queued(lambda op: True)
        tasks: list[asyncio.Task[Any]] = [*self._background]
        if self._in_flight is not None:
            batch, request = self._in_flight
            self._settle(batch)
            tasks.append(request)
        tasks.extend(task for task in (self._sender, self._rebind) if task is not None)
        self._legs.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Gobby-Front-Door": self._secret,
            "X-Gobby-Backend-Token": token,
        }

    async def _await_token(self) -> str:
        # The supervisor may deliver the token after init_servers built the publisher.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TOKEN_WAIT
        while (token := supervisor_write_token()) is None:
            if loop.time() >= deadline:
                raise _BindFailed("no supervisor write token")
            await asyncio.sleep(TOKEN_POLL_INTERVAL)
        return token

    def _enqueue(self, op: dict[str, Any]) -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        generation = self._generation
        if generation is None:
            future.set_result(None)
            return future
        encoded = json_dumps(op).encode()
        # Each operation holds exactly one waiting future, so the op count is the future count.
        if (
            self._retained_bytes + len(encoded) > MAX_RETAINED_BYTES
            or self._retained_ops + 1 > MAX_RETAINED_OPS
            or self._retained_ops + 1 > MAX_WAITING_FUTURES
        ):
            future.set_result(None)
            logger.warning("Native WS publish budget exceeded; failing generation %s", generation)
            self.fail_generation(generation)
            return future
        self._queue.append(_Op(generation, encoded, future))
        self._retained_bytes += len(encoded)
        self._retained_ops += 1
        if self._sender is None or self._sender.done():
            self._sender = asyncio.create_task(self._send_loop())
        self._wake.set()
        return future

    async def _send_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._queue:
                batch = self._take_batch()
                generation = batch[0].generation
                # A separate task, so fail_generation cancels the request and never its caller.
                request = asyncio.create_task(self._post(generation, batch))
                self._in_flight = (batch, request)
                await asyncio.wait({request})
                if not request.cancelled():
                    if request.exception() is None and request.result():
                        self._settle(batch)
                    else:
                        self.fail_generation(generation)
                self._in_flight = None

    def _take_batch(self) -> list[_Op]:
        first = self._queue.popleft()
        batch = [first]
        size = len(first.encoded)
        while self._queue:
            candidate = self._queue[0]
            if candidate.generation != first.generation:
                break
            if size + len(candidate.encoded) > MAX_RETAINED_BYTES:
                break
            size += len(candidate.encoded)
            batch.append(self._queue.popleft())
        return batch

    async def _post(self, generation: str, batch: list[_Op]) -> bool:
        token = supervisor_write_token()
        if token is None:
            logger.warning("Native WS publish has no supervisor write token")
            return False
        body = b"".join(
            (
                b'{"generation":',
                json_dumps(generation).encode(),
                b',"ops":[',
                b",".join(op.encoded for op in batch),
                b"]}",
            )
        )
        try:
            response = await asyncio.wait_for(
                self._client.post(
                    f"{self._base_url}{PUBLISH_PATH}", content=body, headers=self._headers(token)
                ),
                REQUEST_TIMEOUT,
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            logger.warning("Native WS publish failed for generation %s: %r", generation, exc)
            return False
        if response.status_code != 204:
            logger.warning(
                "Native WS publish for generation %s answered %s", generation, response.status_code
            )
            return False
        return True

    def _settle(self, ops: Iterable[_Op]) -> None:
        # Release accounting before resolving, exactly once per operation.
        for op in ops:
            if op.settled:
                continue
            op.settled = True
            self._retained_bytes -= len(op.encoded)
            self._retained_ops -= 1
            if not op.future.done():
                op.future.set_result(None)

    def _drop_queued(self, predicate: Callable[[_Op], bool]) -> None:
        dropped = [op for op in self._queue if predicate(op)]
        self._queue = deque(op for op in self._queue if not predicate(op))
        self._settle(dropped)

    def _retire(self, generation: str) -> None:
        if self._generation == generation:
            self._generation = None
        self._drop_queued(lambda op: op.generation == generation)
        if self._in_flight is not None:
            batch, request = self._in_flight
            if batch[0].generation == generation:
                self._settle(batch)
                request.cancel()
        legs = self._legs.pop(generation, set())
        if legs:
            self._spawn(self._close_legs(legs))

    async def _close_legs(self, legs: set[NativeLeg]) -> None:
        closes = asyncio.gather(
            *(leg.close(1011, "Native publication failed") for leg in legs),
            return_exceptions=True,
        )
        try:
            await asyncio.wait_for(closes, LEG_CLOSE_TIMEOUT)
        except TimeoutError:
            logger.warning("Native WS legs did not close within %ss", LEG_CLOSE_TIMEOUT)

    def _schedule_rebind(self, *, immediate: bool) -> None:
        if self._stopped or (self._rebind is not None and not self._rebind.done()):
            return
        self._rebind = asyncio.create_task(self._rebind_loop(immediate))

    async def _rebind_loop(self, immediate: bool) -> None:
        if not immediate:
            await asyncio.sleep(REBIND_INTERVAL)
        while not self._stopped:
            try:
                await self.bind()
                return
            except (_BindFailed, httpx.HTTPError, TimeoutError) as exc:
                logger.warning("Native WS rebind failed: %s", exc)
            await asyncio.sleep(REBIND_INTERVAL)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)


def native_publisher_from_environment(
    config: WebSocketConfig,
    bootstrap: BootstrapConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> NativePublisher | None:
    """Native mode needs both the supervisor pipe and the front-door pair secret."""
    secret = os.environ.get("GOBBY_FRONT_DOOR_SECRET")
    if not os.environ.get("GOBBY_SUPERVISOR_FD") or not secret:
        return None
    return NativePublisher(
        secret=secret,
        base_url=f"http://{_loopback_host(bootstrap.bind_host)}:{bootstrap.daemon_port}",
        config=config,
        transport=transport,
    )


def _loopback_host(bind_host: str) -> str:
    """Decision 10: IPv6 loopback when bind_host is an IPv6 literal, else IPv4 loopback."""
    try:
        address = ipaddress.ip_address(bind_host.strip().removeprefix("[").removesuffix("]"))
    except ValueError:
        return "127.0.0.1"
    return "[::1]" if address.version == 6 else "127.0.0.1"
