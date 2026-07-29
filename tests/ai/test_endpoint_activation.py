"""Tests for Responses endpoint activation policy."""

import asyncio
from types import SimpleNamespace

import pytest

from gobby.ai import endpoint_activation
from gobby.ai.endpoint_activation import EndpointActivationError, probe_responses_endpoint
from gobby.config.ai import GenerationEndpointConfig
from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit


def _endpoint(*, vision_extract: bool = True) -> GenerationEndpointConfig:
    return GenerationEndpointConfig(
        wire_api="responses",
        api_base="https://openrouter.ai/api/v1",
        api_key="super-secret-key",
        model="moonshotai/kimi-k3",
        tool_chat=True,
        vision_extract=vision_extract,
    )


def test_thread_provider_identity_is_stable_for_endpoint() -> None:
    endpoint_activation._assert_thread_provider(
        SimpleNamespace(model_provider="gobby_endpoint_openrouter"),
        "openrouter",
        phase="thread resume",
    )

    with pytest.raises(
        EndpointActivationError,
        match="expected 'gobby_endpoint_openrouter'",
    ):
        endpoint_activation._assert_thread_provider(
            SimpleNamespace(model_provider="openai"),
            "openrouter",
            phase="thread resume",
        )


@pytest.mark.asyncio
async def test_core_probe_failure_keeps_endpoint_dark_and_redacts_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_text(_name: str, _endpoint: GenerationEndpointConfig) -> None:
        raise RuntimeError("401 invalid super-secret-key")

    monkeypatch.setattr(endpoint_activation, "_probe_text", fail_text)

    with pytest.raises(EndpointActivationError) as exc_info:
        await probe_responses_endpoint("openrouter", _endpoint(), DaemonConfig())

    assert str(exc_info.value) == (
        "Responses endpoint authentication failed; verify the configured secret"
    )
    assert "super-secret-key" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_vision_only_failure_activates_text_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def pass_probe(*_args: object) -> None:
        return None

    async def fail_vision(*_args: object) -> None:
        raise RuntimeError("image input unsupported")

    monkeypatch.setattr(endpoint_activation, "_probe_text", pass_probe)
    monkeypatch.setattr(endpoint_activation, "_probe_tool_context_and_resume", pass_probe)
    monkeypatch.setattr(endpoint_activation, "_probe_vision", fail_vision)

    result = await probe_responses_endpoint("openrouter", _endpoint(), DaemonConfig())

    assert result.vision_enabled is False
    assert result.endpoint.vision_extract is False
    assert result.endpoint.tool_chat is True


@pytest.mark.asyncio
async def test_activation_timeout_covers_complete_serial_probe_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def delayed_probe(name: str) -> None:
        calls.append(name)
        released = asyncio.Event()
        handle = asyncio.get_running_loop().call_later(0.03, released.set)
        try:
            await released.wait()
        finally:
            handle.cancel()

    async def slow_text(*_args: object) -> None:
        await delayed_probe("text")

    async def slow_tool(*_args: object) -> None:
        await delayed_probe("tool")

    async def slow_vision(*_args: object) -> None:
        await delayed_probe("vision")

    monkeypatch.setattr(endpoint_activation, "_probe_text", slow_text)
    monkeypatch.setattr(endpoint_activation, "_probe_tool_context_and_resume", slow_tool)
    monkeypatch.setattr(endpoint_activation, "_probe_vision", slow_vision)

    with pytest.raises(EndpointActivationError, match="timed out after 0.05 seconds"):
        await probe_responses_endpoint(
            "openrouter",
            _endpoint(),
            DaemonConfig(ai={"generation": {"timeout_seconds": 0.05}}),
        )

    assert calls == ["text", "tool"]


@pytest.mark.asyncio
async def test_activation_retries_transient_errors_three_times_and_honors_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("429 rate limited; Retry-After: 120")
        return "ok"

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await endpoint_activation._retry_activation(operation)

    assert result == "ok"
    assert attempts == 3
    assert delays == [60.0, 60.0]


@pytest.mark.asyncio
async def test_activation_surfaces_terminal_429_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("429 rate limited")

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="429 rate limited"):
        await endpoint_activation._retry_activation(operation)

    assert attempts == 3


async def test_activation_does_not_retry_non_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("invalid response contract")

    with pytest.raises(RuntimeError, match="invalid response contract"):
        await endpoint_activation._retry_activation(operation)

    assert calls == 1
