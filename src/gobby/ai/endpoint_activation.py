"""Activation probes for generation endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import struct
import tempfile
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.agents.local_model import resolve_vllm_served_model
from gobby.ai._text_generation_adapters import CodexCLITextGenerateAdapter
from gobby.ai._text_generation_contracts import TextGenerationRequest
from gobby.ai.codex_endpoint import (
    codex_endpoint_app_server_env,
    codex_endpoint_config_overrides,
    codex_endpoint_env,
    codex_endpoint_provider_id,
    codex_event_text,
)
from gobby.ai.vision import CodexEndpointVisionExtractAdapter, VisionExtractRequest
from gobby.config.ai import GenerationEndpointConfig
from gobby.config.app import DaemonConfig
from gobby.llm.local_provider_adapters import create_local_provider_adapter

logger = logging.getLogger(__name__)

_TRANSIENT_STATUS_RE = re.compile(r"\b(?:429|5\d\d)\b")
_RETRY_AFTER_RE = re.compile(r"retry-after\s*[:=]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_AUTH_RE = re.compile(r"\b(?:401|403)\b|auth(?:entication|orization)?|api key", re.IGNORECASE)
_PROBE_TOKEN = "GOBBY_K3_CONTEXT_7F3A"
# Vision probes show a randomly chosen solid-color square and accept only a
# reply that names that color and no other, so a text-only model (or a server
# that silently drops image parts) cannot pass by acknowledging "an image".
_PROBE_COLORS: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("red", (255, 0, 0)),
    ("green", (0, 255, 0)),
    ("blue", (0, 0, 255)),
)
_PROBE_COLOR_NAMES: frozenset[str] = frozenset(name for name, _ in _PROBE_COLORS)
_PROBE_IMAGE_SIZE = 64
_CHAT_PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "gobby_probe_tool",
        "description": "Report that the tool-call probe succeeded.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}


class EndpointActivationError(RuntimeError):
    """A generation endpoint failed its mandatory activation contract."""


def modalities_for_served_model(
    endpoint: GenerationEndpointConfig,
    model: str,
) -> list[str] | None:
    """Return persisted probe modalities for ``model``, else unknown."""
    if endpoint.probed_model is None or endpoint.probed_model != model:
        return None
    if endpoint.input_modalities is None:
        return None
    return list(endpoint.input_modalities)


def _activation_result(
    endpoint: GenerationEndpointConfig,
    *,
    probed_model: str,
    input_modalities: list[str],
    probed_json: bool,
    probed_tools: bool | None,
    diagnostics: dict[str, str],
) -> EndpointActivationResult:
    updated = endpoint.model_copy(
        update={
            "probed_model": probed_model,
            "input_modalities": input_modalities,
            "probed_json": probed_json,
            "probed_tools": probed_tools,
        }
    )
    return EndpointActivationResult(
        endpoint=updated,
        vision_enabled="image" in input_modalities,
        diagnostics=diagnostics,
    )


@dataclass(frozen=True)
class EndpointActivationResult:
    endpoint: GenerationEndpointConfig
    vision_enabled: bool
    diagnostics: dict[str, str] = field(default_factory=dict)


def _client(endpoint_name: str, endpoint: GenerationEndpointConfig) -> CodexAppServerClient:
    return CodexAppServerClient(
        config_overrides=codex_endpoint_config_overrides(endpoint_name, endpoint),
        env_overrides=codex_endpoint_app_server_env(endpoint_name, endpoint),
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
        ignore_user_config=endpoint.protocol == "vllm",
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


async def _probe_json(endpoint_name: str, endpoint: GenerationEndpointConfig) -> None:
    adapter = CodexCLITextGenerateAdapter(
        timeout_seconds=120.0,
        env=codex_endpoint_env(endpoint),
        config_overrides=codex_endpoint_config_overrides(endpoint_name, endpoint),
        ignore_user_config=endpoint.protocol == "vllm",
    )
    text = await adapter.generate(
        TextGenerationRequest(
            prompt='Reply with JSON object {"ok": true} and nothing else.',
            provider="codex",
            model=endpoint.model,
            caller="generation-endpoint-activation",
        )
    )
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise EndpointActivationError("Responses JSON probe returned non-JSON") from exc
    if not isinstance(parsed, dict):
        raise EndpointActivationError("Responses JSON probe returned a non-object")


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
    color, image_bytes = _probe_color()
    with tempfile.TemporaryDirectory(prefix="gobby-endpoint-vision-") as temp_dir:
        image_path = Path(temp_dir) / "probe.png"
        image_path.write_bytes(image_bytes)
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
                _VISION_PROBE_PROMPT,
                images=[str(image_path)],
            )
            _check_vision_probe_reply("Responses", text, color)
        finally:
            await client.stop()

        adapter = CodexEndpointVisionExtractAdapter(daemon_config, endpoint_name)
        try:
            extracted = await adapter.extract(
                VisionExtractRequest(
                    image_path=str(image_path),
                    provider=f"endpoint:{endpoint_name}",
                    model=endpoint.model,
                    context="Describe the solid-color image.",
                )
            )
            if not extracted:
                raise EndpointActivationError("Daemon vision extraction returned no text")
        finally:
            await adapter.stop()


def _redact(message: str, api_key: str) -> str:
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    return message


def _degraded_probe(endpoint_name: str, probe: str, exc: BaseException, api_key: str) -> str:
    """Record a degraded (non-fatal) probe failure and return its redacted message."""
    message = _redact(str(exc), api_key)
    logger.warning(
        "Endpoint %r %s probe failed: %s; activation continues without it",
        endpoint_name,
        probe,
        message,
    )
    return message


def _sanitized_activation_error(
    exc: BaseException,
    api_key: str,
    *,
    transport: str = "Responses",
) -> EndpointActivationError:
    message = _redact(str(exc), api_key)
    if _AUTH_RE.search(message):
        return EndpointActivationError(
            f"{transport} endpoint authentication failed; verify the configured secret"
        )
    return EndpointActivationError(f"{transport} endpoint activation failed: {message}")


async def probe_responses_endpoint(
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
    daemon_config: DaemonConfig,
) -> EndpointActivationResult:
    """Probe the actual Codex contracts before a Responses endpoint is persisted."""
    if endpoint.wire_api != "responses":
        raise ValueError("Activation probing is only required for Responses endpoints")
    api_key = next(iter(codex_endpoint_env(endpoint).values()))

    async def probe_chain() -> EndpointActivationResult:
        diagnostics: dict[str, str] = {}
        try:
            await _retry_activation(lambda: _probe_text(endpoint_name, endpoint))
        except Exception as exc:
            raise _sanitized_activation_error(exc, api_key) from exc

        probed_json = False
        try:
            await _retry_activation(lambda: _probe_json(endpoint_name, endpoint))
            probed_json = True
        except Exception as exc:
            probed_json = False
            diagnostics["json"] = _degraded_probe(endpoint_name, "json", exc, api_key)

        probed_tools: bool | None
        if not endpoint.tool_chat:
            probed_tools = None
        else:
            try:
                await _retry_activation(
                    lambda: _probe_tool_context_and_resume(endpoint_name, endpoint)
                )
                probed_tools = True
            except Exception as exc:
                probed_tools = False
                diagnostics["tools"] = _degraded_probe(endpoint_name, "tools", exc, api_key)

        input_modalities = ["text"]
        try:
            await _retry_activation(lambda: _probe_vision(endpoint_name, endpoint, daemon_config))
            input_modalities = ["text", "image"]
        except Exception as exc:
            input_modalities = ["text"]
            diagnostics["vision"] = _degraded_probe(endpoint_name, "vision", exc, api_key)
        return _activation_result(
            endpoint,
            probed_model=endpoint.model,
            input_modalities=input_modalities,
            probed_json=probed_json,
            probed_tools=probed_tools,
            diagnostics=diagnostics,
        )

    timeout_seconds = daemon_config.ai.generation.timeout_seconds
    try:
        async with asyncio.timeout(timeout_seconds):
            return await probe_chain()
    except TimeoutError as exc:
        raise EndpointActivationError(
            f"Responses endpoint activation timed out after {timeout_seconds:g} seconds"
        ) from exc


async def _resolve_probe_model(endpoint: GenerationEndpointConfig) -> str:
    if endpoint.protocol == "vllm":
        return await resolve_vllm_served_model(endpoint)
    return endpoint.model


async def _probe_chat_text(adapter: Any, model: str) -> None:
    result = await adapter.generate_text_result(
        "Reply with exactly GOBBY_K3_TEXT_OK.",
        system_prompt=None,
        model=model,
        max_tokens=64,
    )
    if "GOBBY_K3_TEXT_OK" not in result.text:
        raise EndpointActivationError("Chat-completions text probe returned an unexpected response")


async def _probe_chat_json(adapter: Any, model: str) -> None:
    parsed = await adapter.generate_json(
        'Reply with JSON {"ok": true} and nothing else.',
        system_prompt="You are a helpful assistant. Respond with valid JSON.",
        model=model,
        max_tokens=64,
        allow_fallback=False,
    )
    if not isinstance(parsed, dict):
        raise EndpointActivationError("Chat-completions JSON probe returned a non-object")


async def _probe_chat_tools(adapter: Any, model: str) -> None:
    client = getattr(adapter, "client", None)
    if client is None:
        raise EndpointActivationError("Endpoint adapter has no client for tool probing")
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Call gobby_probe_tool now."}],
        tools=[_CHAT_PROBE_TOOL],
        tool_choice={
            "type": "function",
            "function": {"name": "gobby_probe_tool"},
        },
        max_tokens=64,
    )
    message = response.choices[0].message
    tool_calls = getattr(message, "tool_calls", None) or []
    if not tool_calls:
        raise EndpointActivationError("Chat-completions tool probe returned no tool call")


async def _probe_chat_vision(adapter: Any, model: str) -> None:
    color, image_bytes = _probe_color()
    with tempfile.TemporaryDirectory(prefix="gobby-endpoint-vision-") as temp_dir:
        image_path = Path(temp_dir) / "probe.png"
        image_path.write_bytes(image_bytes)
        result = await adapter.generate_text_result(
            _VISION_PROBE_PROMPT,
            system_prompt="You are a vision assistant.",
            model=model,
            max_tokens=64,
            images=[str(image_path)],
        )
    _check_vision_probe_reply("Chat-completions", result.text, color)


def _probe_color() -> tuple[str, bytes]:
    """Pick the probe color at random and render its solid PNG."""
    name, rgb = secrets.choice(_PROBE_COLORS)
    return name, _solid_color_png(rgb, _PROBE_IMAGE_SIZE)


def _solid_color_png(rgb: tuple[int, int, int], size: int) -> bytes:
    """Render a ``size``×``size`` 8-bit RGB PNG filled with ``rgb``."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    row = b"\x00" + bytes(rgb) * size
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )


_VISION_PROBE_PROMPT = (
    "The attached image is a single solid color. Reply with exactly one word "
    "naming that color: red, green, or blue."
)


def _vision_probe_reply_matches(text: str, expected: str) -> bool:
    """Accept only a reply naming ``expected`` and no other probe color."""
    named = {word for word in re.findall(r"[a-z]+", text.lower()) if word in _PROBE_COLOR_NAMES}
    return named == {expected}


def _check_vision_probe_reply(transport: str, text: str, expected: str) -> None:
    if not text.strip():
        raise EndpointActivationError(f"{transport} vision probe returned no text")
    if not _vision_probe_reply_matches(text, expected):
        raise EndpointActivationError(
            f"{transport} vision probe did not identify the {expected} image "
            f"(reply: {text.strip()[:80]!r})"
        )


async def probe_chat_completions_endpoint(
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
    daemon_config: DaemonConfig,
) -> EndpointActivationResult:
    """Probe chat-completions endpoints and persist model-scoped evidence."""
    if endpoint.wire_api == "responses":
        raise ValueError("Chat-completions probing is only for chat-completions endpoints")

    async def probe_chain() -> EndpointActivationResult:
        diagnostics: dict[str, str] = {}
        api_key = endpoint.api_key or ""
        resolved_model = await _resolve_probe_model(endpoint)
        adapter = create_local_provider_adapter(endpoint)
        try:
            await _retry_activation(lambda: _probe_chat_text(adapter, resolved_model))
        except Exception as exc:
            raise _sanitized_activation_error(exc, api_key, transport="Chat-completions") from exc

        probed_json = False
        try:
            await _retry_activation(lambda: _probe_chat_json(adapter, resolved_model))
            probed_json = True
        except Exception as exc:
            probed_json = False
            diagnostics["json"] = _degraded_probe(endpoint_name, "json", exc, api_key)

        probed_tools: bool | None
        if not endpoint.tool_chat:
            probed_tools = None
        else:
            try:
                await _retry_activation(lambda: _probe_chat_tools(adapter, resolved_model))
                probed_tools = True
            except Exception as exc:
                probed_tools = False
                diagnostics["tools"] = _degraded_probe(endpoint_name, "tools", exc, api_key)

        input_modalities = ["text"]
        try:
            await _retry_activation(lambda: _probe_chat_vision(adapter, resolved_model))
            input_modalities = ["text", "image"]
        except Exception as exc:
            input_modalities = ["text"]
            diagnostics["vision"] = _degraded_probe(endpoint_name, "vision", exc, api_key)
        return _activation_result(
            endpoint,
            probed_model=resolved_model,
            input_modalities=input_modalities,
            probed_json=probed_json,
            probed_tools=probed_tools,
            diagnostics=diagnostics,
        )

    timeout_seconds = daemon_config.ai.generation.timeout_seconds
    try:
        async with asyncio.timeout(timeout_seconds):
            return await probe_chain()
    except TimeoutError as exc:
        raise EndpointActivationError(
            f"Chat-completions endpoint activation timed out after {timeout_seconds:g} seconds"
        ) from exc
