"""Circuit breaker for the shared feature-LLM route (gobby-#17696).

Under a sustained provider outage every feature call would otherwise run the full
slow try-all-candidates loop before failing; high-volume callers then compound that
latency into a prolonged backlog. The breaker trips a
provider binding after N consecutive failures and short-circuits further calls to
it for a cooldown, so callers fail fast (and fall back) instead of hanging. It is a
no-op in healthy operation: a single success resets the counter, and the open state
is purely time-based so it can never latch permanently.
"""

from __future__ import annotations

import logging

import pytest

from gobby.ai import (
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    TextGenerateAdapter,
    TextGenerationRequest,
    TextGenerationService,
)
from gobby.ai._text_generation_service import _CIRCUIT_BREAKER_MAX_KEYS, _CircuitOpenError

pytestmark = pytest.mark.unit


class _CountingFailAdapter:
    """Always fails; records how many times generate() was actually invoked."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request: TextGenerationRequest) -> str:
        self.calls += 1
        raise RuntimeError("provider degraded")


class _ScriptedAdapter:
    """Fails or succeeds per a fixed script; records real invocations.

    ``outcomes[i]`` True means the i-th invocation raises; missing/False succeeds.
    """

    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def generate(self, request: TextGenerationRequest) -> str:
        index = self.calls
        self.calls += 1
        if index < len(self.outcomes) and self.outcomes[index]:
            raise RuntimeError("provider degraded")
        return "ok"


class _BlankAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request: TextGenerationRequest) -> str:
        self.calls += 1
        return " "


def _breaker_service(
    adapter: TextGenerateAdapter,
    *,
    threshold: int,
    cooldown: float,
) -> TextGenerationService:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
            )
        ]
    )
    return TextGenerationService(
        registry,
        {"claude": adapter},
        circuit_breaker_failure_threshold=threshold,
        circuit_breaker_cooldown_seconds=cooldown,
    )


async def _call(service: TextGenerationService) -> str:
    return await service.generate(
        TextGenerationRequest(prompt="hi", provider="claude", model="haiku")
    )


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold_and_short_circuits() -> None:
    adapter = _CountingFailAdapter()
    service = _breaker_service(adapter, threshold=3, cooldown=60.0)

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await _call(service)
    assert adapter.calls == 3

    # Breaker is open: the next call short-circuits without invoking the adapter.
    with pytest.raises(_CircuitOpenError):
        await _call(service)
    assert adapter.calls == 3


@pytest.mark.asyncio
async def test_breaker_resets_counter_on_success() -> None:
    # fail, fail, SUCCESS, fail, fail. With threshold=3 and a reset-on-success
    # breaker the counter never reaches 3, so every call reaches the adapter.
    # Without the reset, the 4th failure would open the breaker and the 5th call
    # would be short-circuited (adapter.calls would stop at 4).
    adapter = _ScriptedAdapter([True, True, False, True, True])
    service = _breaker_service(adapter, threshold=3, cooldown=60.0)

    for _ in range(5):
        try:
            await _call(service)
        except Exception:
            pass

    assert adapter.calls == 5


@pytest.mark.asyncio
async def test_breaker_is_noop_in_healthy_operation() -> None:
    adapter = _ScriptedAdapter([False] * 10)
    service = _breaker_service(adapter, threshold=2, cooldown=60.0)

    for _ in range(10):
        assert await _call(service) == "ok"
    assert adapter.calls == 10


@pytest.mark.asyncio
async def test_breaker_probes_again_after_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Drive the breaker's clock deterministically instead of sleeping.
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "gobby.ai._text_generation_service.time.monotonic",
        lambda: clock["now"],
    )
    adapter = _CountingFailAdapter()
    service = _breaker_service(adapter, threshold=2, cooldown=30.0)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await _call(service)
    assert adapter.calls == 2

    # Open: an immediate call short-circuits without advancing the clock.
    with pytest.raises(_CircuitOpenError):
        await _call(service)
    assert adapter.calls == 2

    # Advancing past the cooldown lets exactly one probe through.
    clock["now"] += 31.0
    with pytest.raises(RuntimeError):
        await _call(service)
    assert adapter.calls == 3


@pytest.mark.asyncio
async def test_breaker_reopens_immediately_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "gobby.ai._text_generation_service.time.monotonic",
        lambda: clock["now"],
    )
    adapter = _CountingFailAdapter()
    service = _breaker_service(adapter, threshold=2, cooldown=30.0)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await _call(service)
    assert adapter.calls == 2

    clock["now"] += 31.0
    with pytest.raises(RuntimeError):
        await _call(service)
    assert adapter.calls == 3

    with pytest.raises(_CircuitOpenError):
        await _call(service)
    assert adapter.calls == 3


@pytest.mark.asyncio
async def test_breaker_open_logs_single_info_and_debug_rejections(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Opening the breaker logs one INFO transition; rejections stay DEBUG."""
    adapter = _CountingFailAdapter()
    service = _breaker_service(adapter, threshold=2, cooldown=60.0)

    with caplog.at_level(logging.DEBUG, logger="gobby.ai.text_generation"):
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await _call(service)
        for _ in range(3):
            with pytest.raises(_CircuitOpenError):
                await _call(service)

    records = [record for record in caplog.records if record.name == "gobby.ai.text_generation"]
    open_transitions = [
        record
        for record in records
        if record.levelno == logging.INFO and "circuit open for 'claude:haiku'" in record.message
    ]
    assert len(open_transitions) == 1
    assert "after 2 consecutive failures" in open_transitions[0].message

    rejections = [
        record
        for record in records
        if record.getMessage() == "feature_llm_call"
        and "circuit open for 'claude:haiku'" in str(getattr(record, "error", ""))
    ]
    # One DEBUG rejection per short-circuited call, none at ERROR.
    assert len(rejections) == 3
    assert all(record.levelno == logging.DEBUG for record in rejections)


@pytest.mark.asyncio
async def test_breaker_probe_success_logs_close_transition(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful probe after cooldown logs the circuit-closed transition."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "gobby.ai._text_generation_service.time.monotonic",
        lambda: clock["now"],
    )
    adapter = _ScriptedAdapter([True, True])
    service = _breaker_service(adapter, threshold=2, cooldown=30.0)

    with caplog.at_level(logging.DEBUG, logger="gobby.ai.text_generation"):
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await _call(service)
        clock["now"] += 31.0
        assert await _call(service) == "ok"

    close_transitions = [
        record
        for record in caplog.records
        if record.name == "gobby.ai.text_generation"
        and record.levelno == logging.INFO
        and "circuit closed for 'claude:haiku'" in record.message
    ]
    assert len(close_transitions) == 1


@pytest.mark.asyncio
async def test_breaker_disabled_when_threshold_not_positive() -> None:
    adapter = _CountingFailAdapter()
    service = _breaker_service(adapter, threshold=0, cooldown=60.0)

    # With the breaker disabled every call reaches the adapter, even after many
    # consecutive failures.
    for _ in range(6):
        with pytest.raises(RuntimeError):
            await _call(service)
    assert adapter.calls == 6


@pytest.mark.asyncio
async def test_local_validation_errors_do_not_open_breaker() -> None:
    adapter = _BlankAdapter()
    service = _breaker_service(adapter, threshold=1, cooldown=60.0)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await _call(service)

    assert adapter.calls == 2


def test_breaker_state_is_bounded() -> None:
    service = _breaker_service(_CountingFailAdapter(), threshold=2, cooldown=60.0)

    for index in range(_CIRCUIT_BREAKER_MAX_KEYS + 10):
        service._breaker_record_failure(f"provider:model-{index}")

    assert len(service._breaker_failures) <= _CIRCUIT_BREAKER_MAX_KEYS
    assert len(service._breaker_open_until) <= _CIRCUIT_BREAKER_MAX_KEYS


class _CooldownError(RuntimeError):
    """A provider failure that reports a ``retry_after`` cooldown (like a rate limit)."""

    def __init__(self, message: str, retry_after: object) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class _RetryAfterFailAdapter:
    """Fails with a provider exception carrying a ``retry_after`` cooldown."""

    def __init__(self, retry_after: float) -> None:
        self.calls = 0
        self._retry_after = retry_after

    async def generate(self, request: TextGenerationRequest) -> str:
        self.calls += 1
        raise _CooldownError("provider rate-limited", self._retry_after)


async def test_reported_retry_after_opens_breaker_before_threshold() -> None:
    adapter = _RetryAfterFailAdapter(retry_after=300.0)
    service = _breaker_service(adapter, threshold=8, cooldown=60.0)

    # One rate-limit failure that reports a reset window opens the breaker
    # immediately, without waiting for the consecutive-failure threshold.
    with pytest.raises(RuntimeError):
        await _call(service)
    assert adapter.calls == 1

    with pytest.raises(_CircuitOpenError):
        await _call(service)
    assert adapter.calls == 1


def test_retry_after_from_exception_reads_positive_float() -> None:
    from gobby.ai._text_generation_service import _retry_after_from_exception

    assert _retry_after_from_exception(RuntimeError("x")) is None
    assert _retry_after_from_exception(_CooldownError("x", 0.0)) is None
    assert _retry_after_from_exception(_CooldownError("x", 42.0)) == 42.0
    # A bool is not a valid duration even though it is an int subtype.
    assert _retry_after_from_exception(_CooldownError("x", True)) is None
