"""Long-lived stream pumps must stay off the loop's default executor.

Every ``asyncio.to_thread`` call in the daemon shares one ``ThreadPoolExecutor``
that is only ``min(32, cpu_count + 4)`` threads wide. The Codex app-server
client parks two of those threads per live client -- one in ``stdout.readline``
and one in the stderr drain's ``os.read`` -- and neither returns until the
child process exits. Those slots are gone for the daemon's whole life, and
short offloads queue behind whatever is left. That is what pushed
``GET /api/health`` past the hook client's five-second timeout (#20839).

Each test below pins the default executor to a single thread, starts one real
pump against a stream nothing ever writes to, and then asks for an ordinary
``asyncio.to_thread`` hop. A pump that parked the shared worker cannot answer.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import IO

import pytest

from gobby.adapters.codex_impl.client_lifecycle import stop
from gobby.adapters.codex_impl.client_rpc import read_loop
from gobby.adapters.subprocess_stderr import SubprocessStderrDrain
from gobby.servers.routes.admin._health import create_health_router
from gobby.utils.stream_pump import open_stream_pump_executor

pytestmark = pytest.mark.unit

# Long enough that a genuinely queued hop cannot sneak in under it, short
# enough that a red run fails fast.
OFFLOAD_TIMEOUT_SECONDS = 3.0
# Give the pump time to reach its blocking read before the offload is asked for.
PUMP_SETTLE_SECONDS = 0.2


@pytest.fixture
async def default_executor_with_one_thread() -> AsyncIterator[None]:
    """Shrink the default executor so a single parked pump exhausts it."""
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="probe-default")
    loop.set_default_executor(executor)
    try:
        yield
    finally:
        loop.set_default_executor(ThreadPoolExecutor(thread_name_prefix="probe-restored"))
        executor.shutdown(wait=False)


class QuietPipe:
    """A pipe with a live writer that never writes, so reads block forever."""

    def __init__(self) -> None:
        self.read_fd, self._write_fd = os.pipe()

    def open_text_reader(self) -> IO[str]:
        """Return an independent text stream over the same pipe."""
        return os.fdopen(os.dup(self.read_fd), "r")

    def release_readers(self) -> None:
        """Close the write end so every blocked reader sees EOF and returns."""
        self._close(self._write_fd)

    def close(self) -> None:
        self.release_readers()
        self._close(self.read_fd)

    @staticmethod
    def _close(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass


@pytest.fixture
def quiet_pipe() -> Iterator[QuietPipe]:
    pipe = QuietPipe()
    try:
        yield pipe
    finally:
        pipe.close()


async def assert_default_executor_still_answers() -> None:
    """Fail unless an ordinary offload still gets a shared worker."""
    await asyncio.sleep(PUMP_SETTLE_SECONDS)
    await asyncio.wait_for(
        asyncio.to_thread(threading.current_thread),
        timeout=OFFLOAD_TIMEOUT_SECONDS,
    )


async def test_the_helper_hands_each_pump_a_thread_of_its_own(
    default_executor_with_one_thread: None,
) -> None:
    """Two pumps parked at once still leave the default executor untouched."""
    loop = asyncio.get_running_loop()
    first = open_stream_pump_executor("first")
    second = open_stream_pump_executor("second")
    release = threading.Event()
    parked = [
        loop.run_in_executor(first, release.wait),
        loop.run_in_executor(second, release.wait),
    ]
    try:
        await assert_default_executor_still_answers()
    finally:
        release.set()
        await asyncio.gather(*parked)
        first.shutdown(wait=True)
        second.shutdown(wait=True)


async def test_a_quiet_stderr_drain_leaves_the_default_executor_free(
    default_executor_with_one_thread: None,
    quiet_pipe: QuietPipe,
) -> None:
    """A codex stderr drain waits on its own thread, not a shared one."""
    stream = quiet_pipe.open_text_reader()
    drain = SubprocessStderrDrain("codex-app-server")
    drain.start_text(stream)
    try:
        await assert_default_executor_still_answers()
    finally:
        quiet_pipe.release_readers()
        await drain.stop()
        stream.close()


async def test_a_quiet_codex_read_loop_leaves_the_default_executor_free(
    default_executor_with_one_thread: None,
    quiet_pipe: QuietPipe,
) -> None:
    """The codex stdout reader waits on its own thread, not a shared one."""
    stream = quiet_pipe.open_text_reader()
    client = SimpleNamespace(
        _process=SimpleNamespace(stdout=stream, poll=lambda: 0),
        _shutdown_event=asyncio.Event(),
    )
    task = asyncio.create_task(read_loop(client))  # type: ignore[arg-type]
    try:
        await assert_default_executor_still_answers()
    finally:
        client._shutdown_event.set()
        quiet_pipe.release_readers()
        await asyncio.gather(task, return_exceptions=True)
        stream.close()


async def test_reaping_a_stubborn_codex_child_leaves_the_default_executor_free(
    default_executor_with_one_thread: None,
) -> None:
    """A child that ignores SIGTERM holds its own thread for the five-second wait."""
    exited = threading.Event()
    process = SimpleNamespace(
        terminate=lambda: None,
        kill=exited.set,
        wait=lambda: exited.wait(),
        stdin=None,
        stdout=None,
        stderr=None,
    )
    client = SimpleNamespace(
        _shutdown_event=asyncio.Event(),
        _reader_task=None,
        _incoming_request_tasks=[],
        _process=process,
        _stderr_drain=SubprocessStderrDrain("codex-app-server"),
        _pending_requests_lock=threading.Lock(),
        _pending_requests={},
    )
    stopping = asyncio.create_task(stop(client))  # type: ignore[arg-type]
    try:
        await assert_default_executor_still_answers()
    finally:
        exited.set()
        await asyncio.gather(stopping, return_exceptions=True)


async def test_liveness_answers_while_the_default_executor_is_exhausted(
    default_executor_with_one_thread: None,
) -> None:
    """Liveness must not wait on a shared worker to read a local stamp file."""
    loop = asyncio.get_running_loop()
    release = threading.Event()
    hog = loop.run_in_executor(None, release.wait)
    router = create_health_router(SimpleNamespace(get_runner=lambda: None))  # type: ignore[arg-type]
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/health")  # type: ignore[attr-defined]
    try:
        payload = await asyncio.wait_for(endpoint(), timeout=OFFLOAD_TIMEOUT_SECONDS)
        assert payload["status"] in {"ok", "degraded"}
    finally:
        release.set()
        await hog
