"""Activation probes for Codex-backed Responses generation endpoints."""

from __future__ import annotations

import asyncio
import base64
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.ai._text_generation_adapters import CodexCLITextGenerateAdapter
from gobby.ai._text_generation_contracts import TextGenerationRequest
from gobby.ai.codex_endpoint import (
    codex_endpoint_config_overrides,
    codex_endpoint_env,
    codex_endpoint_provider_id,
    codex_event_text,
)
from gobby.ai.vision import CodexEndpointVisionExtractAdapter, VisionExtractRequest
from gobby.config.ai import GenerationEndpointConfig
from gobby.config.app import DaemonConfig

_TRANSIENT_STATUS_RE = re.compile(r"\b(?:429|5\d\d)\b")
_RETRY_AFTER_RE = re.compile(r"retry-after\s*[:=]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_AUTH_RE = re.compile(r"\b(?:401|403)\b|auth(?:entication|orization)?|api key", re.IGNORECASE)
_PROBE_TOKEN = "GOBBY_K3_CONTEXT_7F3A"
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nQAAAABJRU5ErkJggg=="
)


class EndpointActivationError(RuntimeError):
    """A Responses endpoint failed its mandatory activation contract."""


@dataclass(frozen=True)
class EndpointActivationResult:
    endpoint: GenerationEndpointConfig
    vision_enabled: bool


def _client(endpoint_name: str, endpoint: GenerationEndpointConfig) -> CodexAppServerClient:
    return CodexAppServerClient(
        config_overrides=codex_endpoint_config_overrides(endpoint_name, endpoint),
        env_overrides=codex_endpoint_env(endpoint),
        global_args=("--ignore-user-config",),
    )


async def _retry_activation[T](operation: Callable[[], Awaitable[T]]) -> T:
    last_error: BaseException | None = None
    for attempt in range(3):
        try:
            return await operation()
        except Exception as exc:
            last_error = exc
            message = str(exc)
            if attempt == 2 or _TRANSIENT_STATUS_RE.search(message) is None:
                raise
            retry_after = _RETRY_AFTER_RE.search(message)
            delay = min(float(retry_after.group(1)), 60.0) if retry_after else 1.0
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


async def _probe_text(endpoint_name: str, endpoint: GenerationEndpointConfig) -> None:
    adapter = CodexCLITextGenerateAdapter(
        timeout_seconds=120.0,
        env=codex_endpoint_env(endpoint),
        config_overrides=codex_endpoint_config_overrides(endpoint_name, endpoint),
    )
    text = await adapter.generate(
        TextGenerationRequest(
            prompt="Reply with exactly GOBBY_K3_TEXT_OK.",
            provider="codex",
            model=endpoint.model,
            caller="generation-endpoint-activation",
        )
    )
    if "GOBBY_K3_TEXT_OK" not in text:
        raise EndpointActivationError("Responses text probe returned an unexpected response")


async def _collect_turn(
    client: CodexAppServerClient,
    thread_id: str,
    prompt: str,
    *,
    images: list[str] | None = None,
) -> tuple[str, bool, bool]:
    deltas: list[str] = []
    completed: list[str] = []
    tool_started = False
    tool_completed = False
    async for event in client.run_turn(thread_id, prompt, images=images):
        event_type = event.get("type")
        item = event.get("item")
        item_type = item.get("type") if isinstance(item, dict) else None
        is_tool = item_type not in {None, "agentMessage", "reasoning"}
        if event_type == "item/started" and is_tool:
            tool_started = True
        if event_type == "item/completed" and is_tool:
            tool_completed = True
        text = codex_event_text(event)
        if event_type == "item/agentMessage/delta" and text:
            deltas.append(text)
        elif event_type == "item/completed" and text:
            completed.append(text)
    return "".join(deltas).strip() or "".join(completed).strip(), tool_started, tool_completed


def _assert_thread_provider(
    thread: object,
    endpoint_name: str,
    *,
    phase: str,
) -> None:
    expected = codex_endpoint_provider_id(endpoint_name)
    actual = getattr(thread, "model_provider", None)
    if actual != expected:
        raise EndpointActivationError(
            f"Responses {phase} returned modelProvider={actual!r}; expected {expected!r}"
        )


async def _probe_tool_context_and_resume(
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
) -> None:
    first = _client(endpoint_name, endpoint)
    thread_id: str | None = None
    try:
        await first.start()
        thread = await first.start_thread(
            model=endpoint.model,
            approval_policy="never",
            sandbox="read-only",
        )
        _assert_thread_provider(thread, endpoint_name, phase="thread start")
        thread_id = thread.id
        text, tool_started, tool_completed = await _collect_turn(
            first,
            thread.id,
            (
                "Use the shell tool to run `printf GOBBY_K3_TOOL_OK`. Then remember "
                f"the token {_PROBE_TOKEN} and reply with both outputs."
            ),
        )
        if not tool_started or not tool_completed or "GOBBY_K3_TOOL_OK" not in text:
            raise EndpointActivationError("Responses tool call/result probe failed")
        second, _, _ = await _collect_turn(
            first,
            thread.id,
            "Repeat the token I asked you to remember, with no other text.",
        )
        if _PROBE_TOKEN not in second:
            raise EndpointActivationError("Responses second-turn context probe failed")
    finally:
        await first.stop()

    if thread_id is None:
        raise EndpointActivationError("Responses probe did not create a thread")
    resumed = _client(endpoint_name, endpoint)
    try:
        await resumed.start()
        resumed_thread = await resumed.resume_thread(thread_id)
        _assert_thread_provider(resumed_thread, endpoint_name, phase="thread resume")
        text, _, _ = await _collect_turn(
            resumed,
            thread_id,
            "After this process restart, repeat the remembered token only.",
        )
        if _PROBE_TOKEN not in text:
            raise EndpointActivationError("Responses restart/resume context probe failed")
    finally:
        await resumed.stop()


async def _probe_vision(
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
    daemon_config: DaemonConfig,
) -> None:
    with tempfile.TemporaryDirectory(prefix="gobby-endpoint-vision-") as temp_dir:
        image_path = Path(temp_dir) / "probe.png"
        image_path.write_bytes(_PNG_1X1)
        client = _client(endpoint_name, endpoint)
        try:
            await client.start()
            thread = await client.start_thread(
                model=endpoint.model,
                approval_policy="never",
                sandbox="read-only",
                ephemeral=True,
            )
            text, _, _ = await _collect_turn(
                client,
                thread.id,
                "State that you received an image.",
                images=[str(image_path)],
            )
            if not text:
                raise EndpointActivationError("Responses image-input probe returned no text")
        finally:
            await client.stop()

        adapter = CodexEndpointVisionExtractAdapter(daemon_config, endpoint_name)
        try:
            extracted = await adapter.extract(
                VisionExtractRequest(
                    image_path=str(image_path),
                    provider=f"endpoint:{endpoint_name}",
                    model=endpoint.model,
                    context="Describe the single-pixel image.",
                )
            )
            if not extracted:
                raise EndpointActivationError("Daemon vision extraction returned no text")
        finally:
            await adapter.stop()


def _sanitized_activation_error(exc: BaseException, api_key: str) -> EndpointActivationError:
    message = str(exc).replace(api_key, "[REDACTED]")
    if _AUTH_RE.search(message):
        return EndpointActivationError(
            "Responses endpoint authentication failed; verify the configured secret"
        )
    return EndpointActivationError(f"Responses endpoint activation failed: {message}")


async def probe_responses_endpoint(
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
    daemon_config: DaemonConfig,
) -> EndpointActivationResult:
    """Probe the actual Codex contracts before a Responses endpoint is persisted."""
    if endpoint.wire_api != "responses":
        raise ValueError("Activation probing is only required for Responses endpoints")
    api_key = next(iter(codex_endpoint_env(endpoint).values()))
    try:
        await _retry_activation(lambda: _probe_text(endpoint_name, endpoint))
        await _retry_activation(lambda: _probe_tool_context_and_resume(endpoint_name, endpoint))
    except Exception as exc:
        raise _sanitized_activation_error(exc, api_key) from exc

    if not endpoint.vision_extract:
        return EndpointActivationResult(endpoint=endpoint, vision_enabled=False)
    try:
        await _retry_activation(lambda: _probe_vision(endpoint_name, endpoint, daemon_config))
    except Exception:
        text_only = endpoint.model_copy(update={"vision_extract": False})
        return EndpointActivationResult(endpoint=text_only, vision_enabled=False)
    return EndpointActivationResult(endpoint=endpoint, vision_enabled=True)
