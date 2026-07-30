"""The rule-engine offload helper must be a drop-in for asyncio.to_thread."""

from __future__ import annotations

import contextvars
import threading

import pytest

from gobby.workflows.engine._offload import ENGINE_EXECUTOR_THREAD_PREFIX, offload

pytestmark = pytest.mark.unit


async def test_offload_runs_on_dedicated_engine_thread() -> None:
    def current_thread_name() -> str:
        return threading.current_thread().name

    name = await offload(current_thread_name)

    assert name.startswith(ENGINE_EXECUTOR_THREAD_PREFIX)


async def test_offload_propagates_contextvars_like_to_thread() -> None:
    var: contextvars.ContextVar[str] = contextvars.ContextVar("engine_offload_probe")
    var.set("caller-value")

    assert await offload(var.get) == "caller-value"


async def test_offload_passes_args_kwargs_and_returns() -> None:
    def combine(base: str, *, suffix: str) -> str:
        return f"{base}-{suffix}"

    assert await offload(combine, "left", suffix="right") == "left-right"


async def test_offload_propagates_exceptions() -> None:
    def boom() -> None:
        raise RuntimeError("engine offload failure")

    with pytest.raises(RuntimeError, match="engine offload failure"):
        await offload(boom)
