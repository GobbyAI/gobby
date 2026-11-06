"""Python's half of native WS publication: bind to gdaemon and publish broadcasts in order."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Coroutine, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from gobby.runner_backend import front_door_secret, supervisor_fd, supervisor_write_token
from gobby.terminals.ws_protocol import CANONICAL_PIECE_CHARS
from gobby.utils.datetime import to_json_safe
from gobby.utils.json_helpers import json_dumps

if TYPE_CHECKING:
    from gobby.config.bootstrap import BootstrapConfig
    from gobby.servers.websocket.models import WebSocketConfig

logger = logging.getLogger(__name__)

# Decision 14 admission budgets. The count budgets cover queued and in-flight operations, plus
# one place for each call still preparing. The byte budget covers queued and in-flight encoded
# bytes, plus the source bytes of calls still preparing. It also bounds the body of any one request.
MAX_RETAINED_BYTES = 128 * 1024 * 1024
MAX_RETAINED_OPS = 65_536
MAX_WAITING_FUTURES = 65_536
# Mitigation, not a demonstrated root cause of the first publish timeout. REQUEST_TIMEOUT
# covers the whole POST, body upload included, so a backlog sized by the retained budget
# can exceed it. Capping each POST bounds its latency; one operation larger than the limit
# still goes alone.
MAX_BATCH_BYTES = 1024 * 1024
# A publish call splits into ops of at most this many encoded bytes, so no request body grows
# with the call. The cap sits above MAX_BATCH_BYTES because each subscriber queues 16 batches
# (gdaemon broker.rs MAX_QUEUED_BATCHES); finer ops could retire a slow subscriber on a burst
# that one op accepts today. A frame above the cap goes alone in its own op.
PUBLISH_CHUNK = 4 * 1024 * 1024

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
        # The secret and token go only to loopback: no proxy or .netrc from the environment.
        self._client = httpx.AsyncClient(transport=transport, trust_env=False)
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
        # The place the latest publish or subscribe took in the queue order. Each call enqueues
        # after the place before it, so ops keep the order their calls were made in.
        self._turn: asyncio.Future[None] | None = None
        # Calls still waiting for their turn or encoding, keyed by their turn. A call leaves once
        # it enqueues, so queued ops are not counted twice and stop can cancel only these.
        self._preparing: dict[asyncio.Future[None], asyncio.Task[None]] = {}
        # Source bytes each preparing call reserved at admission, released at its exchange.
        self._reserved: dict[asyncio.Future[None], int] = {}
        self._reserved_bytes = 0

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
            logger.warning("Native WS bind failed; retrying every %ss: %r", REBIND_INTERVAL, exc)
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
        self,
        message: dict[str, Any],
        frames: Sequence[dict[str, Any]] = (),
        *,
        source_bytes: int | None = None,
    ) -> asyncio.Future[None]:
        """Queue a publish of the frames, or of the message when there are none.

        ``source_bytes`` is the size of the payload the frames are encoded from. It is reserved
        from admission until the call's exchange, so pending payloads count against
        MAX_RETAINED_BYTES. Without frames the message is its own payload, and its filter JSON
        gives its size.
        """
        # Frames are encoded exactly as BroadcastMixin.broadcast encodes them. The filter
        # repeats in every op this call splits into, so callers pass routing fields only.
        # Every op is encoded before any is queued, so a frame that fails to encode queues none.
        filter_json = json_dumps(message)
        if source_bytes is None:
            if frames:
                raise TypeError("publish with frames needs source_bytes, their encoding source")
            source_bytes = len(filter_json)
        return self._sequence(_publish_ops(filter_json, frames or (message,)), source_bytes)

    def subscribe(self, leg: int, events: Iterable[str]) -> asyncio.Future[None]:
        encoded = json_dumps({"op": "subscribe", "leg": leg, "events": list(events)}).encode()
        return self._sequence(_ready([encoded]), len(encoded))

    def _sequence(
        self, ops: Coroutine[Any, Any, list[bytes]], source_bytes: int
    ) -> asyncio.Future[None]:
        """Take this call's place in the queue order now, and queue its ops when they are ready.

        Encoding yields to the loop, so a later call can finish encoding first. Each call
        still enqueues after every call made before it, even when an earlier call is cancelled.
        Admission reserves the call's source bytes until its exchange.
        """
        generation = self._generation
        # A call that is waiting to enqueue holds a task, a turn, its ops and its source payload.
        # From admission it counts as one place against the count caps and its source bytes
        # count against the byte cap. Over the caps, it fails the generation before any of that
        # is built.
        waiting = len(self._preparing) + self._retained_ops + 1
        if (
            waiting > MAX_RETAINED_OPS
            or waiting > MAX_WAITING_FUTURES
            or self._retained_bytes + self._reserved_bytes + source_bytes > MAX_RETAINED_BYTES
        ):
            ops.close()
            logger.warning("Native WS publish budget exceeded; failing generation %s", generation)
            self.fail_generation(generation)
            refused: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            refused.set_result(None)
            return refused
        previous = self._turn
        turn: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._turn = turn
        self._reserved[turn] = source_bytes
        self._reserved_bytes += source_bytes
        task = asyncio.ensure_future(self._queue_turn(previous, turn, generation, ops))
        self._preparing[turn] = task
        # A task cancelled before it starts never runs _queue_turn, so this callback ends the call.
        task.add_done_callback(lambda _: self._unprepare(turn, previous, ops))
        return task

    async def _queue_turn(
        self,
        previous: asyncio.Future[None] | None,
        turn: asyncio.Future[None],
        generation: str | None,
        ops: Coroutine[Any, Any, list[bytes]],
    ) -> None:
        try:
            # Shielded: cancelling this call must not cancel the turn it is waiting for.
            if previous is not None:
                await asyncio.shield(previous)
            # The exchange: the caps are checked and the ops queued in one synchronous step.
            futures = self._enqueue(turn, generation, await ops)
        finally:
            self._unprepare(turn, previous, ops)
        await _settle_all(futures)

    def _unprepare(
        self,
        turn: asyncio.Future[None],
        previous: asyncio.Future[None] | None,
        ops: Coroutine[Any, Any, list[bytes]],
    ) -> None:
        """End a call's preparation, and release its place once the place before it is released.

        A cancelled call releases only after its predecessor, so a later call cannot enqueue
        ahead of an earlier one that is still waiting.
        """
        self._preparing.pop(turn, None)
        # Source bytes end at the exchange; the encoded ops hold their bytes from there. Pops once,
        # because this runs both at the end of _queue_turn and from the task's done callback.
        self._reserved_bytes -= self._reserved.pop(turn, 0)
        # Closes the ops of a call that never started; a finished coroutine is unaffected.
        ops.close()
        if previous is None or previous.done():
            _release(turn)
        else:
            previous.add_done_callback(lambda _: _release(turn))

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
        # Calls still preparing are cancelled. Calls already queued settle through _drop_queued.
        tasks: list[asyncio.Task[Any]] = [*self._background, *self._preparing.values()]
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

    def _enqueue(
        self, turn: asyncio.Future[None], generation: str | None, encoded: list[bytes]
    ) -> list[asyncio.Future[None]]:
        """Queue one call's encoded ops if the caps still hold with them, and return their futures.

        The check and the queueing are one synchronous step. Each other call still preparing
        counts as one place and keeps its source bytes reserved. Over the caps, the generation
        fails and none of this call's ops is queued.
        """
        # An op belongs to the generation that was current when its call was made. If that
        # generation has ended since, the call's ops are dropped, as queued ops of an ended one are.
        if generation is None or generation != self._generation:
            return []
        others = len(self._preparing) - 1
        count = others + self._retained_ops + len(encoded)
        reserved_by_others = self._reserved_bytes - self._reserved.get(turn, 0)
        size = sum(len(op) for op in encoded)
        # One op alone, in a request with its wrapper, must fit the byte budget.
        largest = max((len(op) for op in encoded), default=0) + len(_request_body(generation, []))
        if (
            count > MAX_RETAINED_OPS
            or count > MAX_WAITING_FUTURES
            or self._retained_bytes + reserved_by_others + size > MAX_RETAINED_BYTES
            or largest > MAX_RETAINED_BYTES
        ):
            logger.warning("Native WS publish budget exceeded; failing generation %s", generation)
            self.fail_generation(generation)
            return []
        loop = asyncio.get_running_loop()
        futures: list[asyncio.Future[None]] = []
        for op in encoded:
            future: asyncio.Future[None] = loop.create_future()
            self._queue.append(_Op(generation, op, future))
            futures.append(future)
        self._retained_bytes += size
        self._retained_ops += len(encoded)
        if self._sender is None or self._sender.done():
            self._sender = asyncio.create_task(self._send_loop())
        self._wake.set()
        return futures

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
        size = len(_request_body(first.generation, [])) + len(first.encoded)
        limit = min(MAX_BATCH_BYTES, MAX_RETAINED_BYTES)
        while self._queue:
            candidate = self._queue[0]
            if candidate.generation != first.generation:
                break
            # Each further operation adds its bytes and one separating comma.
            if size + 1 + len(candidate.encoded) > limit:
                break
            size += 1 + len(candidate.encoded)
            batch.append(self._queue.popleft())
        return batch

    async def _post(self, generation: str, batch: list[_Op]) -> bool:
        token = supervisor_write_token()
        if token is None:
            logger.warning("Native WS publish has no supervisor write token")
            return False
        body = _request_body(generation, [op.encoded for op in batch])
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self._client.post(
                    f"{self._base_url}{PUBLISH_PATH}", content=body, headers=self._headers(token)
                ),
                REQUEST_TIMEOUT,
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            logger.warning(
                "Native WS publish failed for generation %s after %.2fs with %d ops and "
                "%d body bytes: %r",
                generation,
                time.monotonic() - started,
                len(batch),
                len(body),
                exc,
            )
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
                logger.warning("Native WS rebind failed: %r", exc)
            await asyncio.sleep(REBIND_INTERVAL)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)


def _request_body(generation: str, ops: list[bytes]) -> bytes:
    # One join copies the batch once; joining the ops first would copy them twice.
    parts = [b'{"generation":', json_dumps(generation).encode(), b',"ops":[']
    for index, op in enumerate(ops):
        if index:
            parts.append(b",")
        parts.append(op)
    parts.append(b"]}")
    return b"".join(parts)


def _publish_op(filter_json: str, frames: list[bytes]) -> bytes:
    """Encodes a publish op from frame literals, each already encoded as a JSON string.

    The op is joined once from its pieces, so assembling it holds no copy of the frames.
    """
    pieces = [b'{"op": "publish", "filter": ', filter_json.encode(), b', "frames": [']
    for index, frame in enumerate(frames):
        if index:
            pieces.append(b", ")
        pieces.append(frame)
    pieces.append(b"]}")
    return b"".join(pieces)


# A string in a frame is escaped in slices of this many characters, so each escape call is small.
_LITERAL_SLICE_CHARS = CANONICAL_PIECE_CHARS // 8


def _json_pieces(value: Any) -> Iterator[str]:
    """Yield text pieces whose concatenation is json.dumps(value), for a to_json_safe'd value.

    json escapes each character on its own, so the slices of a string concatenate to the
    string's whole escape. Every other scalar goes to json.dumps unchanged.
    """
    if isinstance(value, str):
        yield '"'
        for start in range(0, len(value), _LITERAL_SLICE_CHARS):
            yield json.dumps(value[start : start + _LITERAL_SLICE_CHARS])[1:-1]
        yield '"'
    elif isinstance(value, dict):
        yield "{"
        for index, (key, item) in enumerate(value.items()):
            if index:
                yield ", "
            yield json.dumps(key)
            yield ": "
            yield from _json_pieces(item)
        yield "}"
    elif isinstance(value, list):
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ", "
            yield from _json_pieces(item)
        yield "]"
    else:
        yield json.dumps(value)


async def _frame_literal(frame: Any) -> bytes:
    """Return json_dumps(json_dumps(frame)).encode(), encoding it in pieces that yield the loop.

    The inner text is json_dumps(frame). Its pieces are escaped again for the string literal,
    so the literal is the escape of each piece in turn. It pauses about every
    CANONICAL_PIECE_CHARS of literal output.
    """
    pieces: list[bytes] = [b'"']
    pending = 0
    for piece in _json_pieces(to_json_safe(frame)):
        escaped = json.dumps(piece)[1:-1].encode()
        pieces.append(escaped)
        pending += len(escaped)
        if pending >= CANONICAL_PIECE_CHARS:
            pending = 0
            await asyncio.sleep(0)
    pieces.append(b'"')
    return b"".join(pieces)


async def _publish_ops(filter_json: str, sources: Sequence[dict[str, Any]]) -> list[bytes]:
    """Encodes the frames in order into publish ops, each within PUBLISH_CHUNK bytes.

    A frame is encoded as JSON and then as the JSON string literal its op carries. Each
    literal is dropped once its op is built, so no whole-payload list of frames is held.
    The loop is yielded every CANONICAL_PIECE_CHARS of literal output and after each frame.
    """
    empty = len(_publish_op(filter_json, []))
    ops: list[bytes] = []
    chunk: list[bytes] = []
    size = empty
    for source in sources:
        literal = await _frame_literal(source)
        # The literal is ASCII, so its length is its encoded size.
        # Each frame costs its encoded bytes plus the separator before it.
        cost = len(literal) + 2
        if chunk and size + cost > PUBLISH_CHUNK:
            ops.append(_publish_op(filter_json, chunk))
            chunk = []
            size = empty
        chunk.append(literal)
        size += cost
        await asyncio.sleep(0)
    ops.append(_publish_op(filter_json, chunk))
    return ops


async def _settle_all(futures: list[asyncio.Future[None]]) -> None:
    await asyncio.gather(*futures)


async def _ready(ops: list[bytes]) -> list[bytes]:
    return ops


def _release(turn: asyncio.Future[None]) -> None:
    if not turn.done():
        turn.set_result(None)


def native_publisher_from_environment(
    config: WebSocketConfig,
    bootstrap: BootstrapConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> NativePublisher | None:
    """Native mode needs both the supervisor pipe and the front-door pair secret."""
    secret = front_door_secret()
    if supervisor_fd() is None or not secret:
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
