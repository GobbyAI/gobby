"""Provider-specific transports for local LLM endpoints."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from gobby.config.ai import GenerationEndpointConfig
from gobby.llm.base import (
    LLMProviderError,
    LLMTextResult,
    VisionInputError,
    VisionProviderError,
    VisionProviderUnavailableError,
    validate_vision_description,
)
from gobby.llm.image_payloads import prepare_image_data

logger = logging.getLogger(__name__)

_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
)
_UNSUPPORTED_PARAMETER_MARKERS = (
    "unsupported",
    "not supported",
    "unknown parameter",
    "unrecognized",
    "unexpected keyword",
)
_LOCAL_OPENAI_OVERALL_TIMEOUT_SECONDS = 120.0
_LOCAL_OPENAI_CONNECT_TIMEOUT_SECONDS = 5.0
_LOCAL_OPENAI_READ_TIMEOUT_SECONDS = 120.0
_LOCAL_OPENAI_WRITE_TIMEOUT_SECONDS = 30.0
_LOCAL_OPENAI_POOL_TIMEOUT_SECONDS = 5.0
_LOCAL_OPENAI_MAX_RETRIES = 0
_LOCAL_OPENAI_TIMEOUT = httpx.Timeout(
    _LOCAL_OPENAI_OVERALL_TIMEOUT_SECONDS,
    connect=_LOCAL_OPENAI_CONNECT_TIMEOUT_SECONDS,
    read=_LOCAL_OPENAI_READ_TIMEOUT_SECONDS,
    write=_LOCAL_OPENAI_WRITE_TIMEOUT_SECONDS,
    pool=_LOCAL_OPENAI_POOL_TIMEOUT_SECONDS,
)


class LocalProviderAdapter(Protocol):
    """Transport for one local endpoint provider."""

    @property
    def client(self) -> Any | None:
        """Return the underlying SDK client when one exists."""

    async def generate_text_result(
        self,
        prompt: str,
        *,
        system_prompt: str | None,
        model: str,
        max_tokens: int | None,
        reasoning_effort: str | None = None,
    ) -> LLMTextResult:
        """Generate text with usage metadata."""

    async def generate_json(
        self,
        prompt: str,
        *,
        system_prompt: str | None,
        model: str,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Generate and parse JSON."""

    async def describe_image(
        self,
        image_path: str,
        *,
        context: str | None,
        model: str,
    ) -> str:
        """Describe an image with the local endpoint."""


def create_local_provider_adapter(
    endpoint: GenerationEndpointConfig,
) -> LocalProviderAdapter:
    """Build the configured local provider adapter."""
    if endpoint.protocol in {"openai-compatible", "vllm"}:
        return OpenAICompatibleLocalProviderAdapter(endpoint)
    if endpoint.protocol == "lmstudio":
        return LMStudioLocalProviderAdapter(endpoint)
    if endpoint.protocol == "ollama":
        return OllamaLocalProviderAdapter(endpoint)
    raise ValueError(f"Unsupported generation endpoint protocol: {endpoint.protocol}")


def _usage_dict(usage: Any) -> dict[str, int] | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        data = usage
    elif hasattr(usage, "model_dump"):
        data = usage.model_dump()
    else:
        data = {field: getattr(usage, field, None) for field in _USAGE_FIELDS}

    result = {
        field: value
        for field, value in data.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }
    return result or None


def _is_unsupported_json_mode_error(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    if getattr(response, "status_code", None) != 400:
        return False

    parameter = str(getattr(error, "param", "") or "").lower()
    code = str(getattr(error, "code", "") or "").lower().replace("_", " ")
    code_rejects_parameter = any(marker in code for marker in _UNSUPPORTED_PARAMETER_MARKERS) and (
        parameter == "response_format" or "response format" in code
    )
    message = str(error).lower()
    mentions_json_mode = (
        "response_format" in message or "json_object" in message or "json mode" in message
    )
    message_rejection = mentions_json_mode and any(
        marker in message for marker in _UNSUPPORTED_PARAMETER_MARKERS
    )
    return code_rejects_parameter or message_rejection


def _strip_json_fences(content: str) -> str:
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def _parse_json_response(content: str | None) -> dict[str, Any]:
    if not content:
        raise ValueError("Empty response from local LLM")
    try:
        result: dict[str, Any] = json.loads(_strip_json_fences(content))
        return result
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse local LLM response as JSON: {exc}") from exc


def _origin(api_base: str) -> str:
    normalized = api_base.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return normalized


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _ollama_usage(payload: dict[str, Any]) -> dict[str, int] | None:
    prompt_tokens = payload.get("prompt_eval_count")
    completion_tokens = payload.get("eval_count")
    usage: dict[str, int] = {}
    if isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool):
        usage["prompt_tokens"] = prompt_tokens
    if isinstance(completion_tokens, int) and not isinstance(completion_tokens, bool):
        usage["completion_tokens"] = completion_tokens
    if usage:
        usage["total_tokens"] = usage.get("prompt_tokens", 0) + usage.get(
            "completion_tokens",
            0,
        )
    return usage or None


def _lmstudio_usage(payload: dict[str, Any]) -> dict[str, int] | None:
    stats = payload.get("stats")
    if not isinstance(stats, dict):
        return None
    input_tokens = stats.get("input_tokens")
    output_tokens = stats.get("total_output_tokens")
    usage: dict[str, int] = {}
    if isinstance(input_tokens, int) and not isinstance(input_tokens, bool):
        usage["prompt_tokens"] = input_tokens
    if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
        usage["completion_tokens"] = output_tokens
    if usage:
        usage["total_tokens"] = usage.get("prompt_tokens", 0) + usage.get(
            "completion_tokens",
            0,
        )
    return usage or None


class OpenAICompatibleLocalProviderAdapter:
    """Local provider backed by the OpenAI-compatible SDK path."""

    def __init__(self, endpoint: GenerationEndpointConfig) -> None:
        self._endpoint = endpoint
        self._client: Any | None = None
        api_key = endpoint.api_key or "not-needed"
        try:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                base_url=endpoint.api_base,
                api_key=api_key,
                timeout=_LOCAL_OPENAI_TIMEOUT,
                max_retries=_LOCAL_OPENAI_MAX_RETRIES,
            )
            logger.debug(
                "OpenAI-compatible local provider initialised (url=%s, model=%s)",
                endpoint.api_base,
                endpoint.model,
            )
        except ImportError:
            logger.error("openai package not found - install with: uv add openai")
        except Exception as exc:
            logger.error("Failed to initialise local LLM client: %s", exc)

    @property
    def client(self) -> Any | None:
        return self._client

    async def generate_text_result(
        self,
        prompt: str,
        *,
        system_prompt: str | None,
        model: str,
        max_tokens: int | None,
        reasoning_effort: str | None = None,
    ) -> LLMTextResult:
        if not self._client:
            raise RuntimeError("Local LLM client not initialised")

        request: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt or "You are a helpful assistant.",
                },
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens or 8000,
        }
        if reasoning_effort is not None:
            request["reasoning_effort"] = reasoning_effort
        response = await self._client.chat.completions.create(**request)
        content = response.choices[0].message.content
        if not content or not content.strip():
            raise LLMProviderError(
                "OpenAI-compatible provider "
                f"at {self._endpoint.api_base!r} using model {model!r} returned blank content"
            )
        return LLMTextResult(
            text=content,
            usage=_usage_dict(getattr(response, "usage", None)),
        )

    async def generate_json(
        self,
        prompt: str,
        *,
        system_prompt: str | None,
        model: str,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        if not self._client:
            raise RuntimeError("Local LLM client not initialised")

        request: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                    or "You are a helpful assistant. Respond with valid JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens if max_tokens is not None else 8000,
            "response_format": {"type": "json_object"},
        }
        if reasoning_effort is not None:
            request["reasoning_effort"] = reasoning_effort
        from openai import BadRequestError

        try:
            response = await self._client.chat.completions.create(**request)
        except BadRequestError as json_mode_err:
            if not _is_unsupported_json_mode_error(json_mode_err):
                raise
            logger.debug(
                "json_object mode rejected (%s), retrying without response_format",
                json_mode_err,
            )
            request.pop("response_format", None)
            request["messages"][0]["content"] = (
                system_prompt or "You are a helpful assistant. Respond with valid JSON only."
            )
            response = await self._client.chat.completions.create(**request)

        return _parse_json_response(response.choices[0].message.content)

    async def describe_image(
        self,
        image_path: str,
        *,
        context: str | None,
        model: str,
    ) -> str:
        if not self._client:
            raise VisionProviderUnavailableError("Local LLM client not initialised")

        try:
            _, mime_type, encoded, _ = await prepare_image_data(image_path, logger)
            prompt = _image_prompt(context)
            response = await self._client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{encoded}",
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                max_tokens=1024,
            )
            return validate_vision_description(response.choices[0].message.content or "")
        except (VisionInputError, VisionProviderError):
            raise
        except Exception as exc:
            logger.error("Failed to describe image with local LLM: %s", exc)
            raise VisionProviderError(f"Local image description failed: {exc}") from exc


class LMStudioLocalProviderAdapter:
    """Local provider backed by LM Studio's native REST API."""

    def __init__(self, endpoint: GenerationEndpointConfig) -> None:
        self._endpoint = endpoint
        self._origin = _origin(endpoint.api_base)
        self._headers = _headers(endpoint.api_key)

    @property
    def client(self) -> Any | None:
        return None

    async def generate_text_result(
        self,
        prompt: str,
        *,
        system_prompt: str | None,
        model: str,
        max_tokens: int | None,
        reasoning_effort: str | None = None,
    ) -> LLMTextResult:
        payload: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "system_prompt": system_prompt or "You are a helpful assistant.",
            "stream": False,
            "store": False,
            "max_output_tokens": max_tokens or 8000,
        }
        data = await self._post_chat(payload, timeout=300.0)
        return LLMTextResult(text=_lmstudio_text(data), usage=_lmstudio_usage(data))

    async def generate_json(
        self,
        prompt: str,
        *,
        system_prompt: str | None,
        model: str,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        result = await self.generate_text_result(
            prompt,
            system_prompt=system_prompt
            or "You are a helpful assistant. Respond with valid JSON only.",
            model=model,
            max_tokens=max_tokens if max_tokens is not None else 8000,
            reasoning_effort=reasoning_effort,
        )
        return _parse_json_response(result.text)

    async def describe_image(
        self,
        image_path: str,
        *,
        context: str | None,
        model: str,
    ) -> str:
        try:
            _, _, _, data_url = await prepare_image_data(image_path, logger)
            payload: dict[str, Any] = {
                "model": model,
                "input": [
                    {"type": "image", "data_url": data_url},
                    {"type": "message", "content": _image_prompt(context)},
                ],
                "stream": False,
                "store": False,
                "max_output_tokens": 1024,
            }
            result = _lmstudio_text(await self._post_chat(payload, timeout=300.0))
            return validate_vision_description(result)
        except (VisionInputError, VisionProviderError):
            raise
        except Exception as exc:
            logger.error("Failed to describe image with LM Studio: %s", exc)
            raise VisionProviderError(f"LM Studio image description failed: {exc}") from exc

    async def _post_chat(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._origin}/api/v1/chat",
                headers=self._headers,
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise ValueError("LM Studio returned a non-object response")
        return data


class OllamaLocalProviderAdapter:
    """Local provider backed by Ollama's native REST API."""

    def __init__(self, endpoint: GenerationEndpointConfig) -> None:
        self._endpoint = endpoint
        self._origin = _origin(endpoint.api_base)

    @property
    def client(self) -> Any | None:
        return None

    async def generate_text_result(
        self,
        prompt: str,
        *,
        system_prompt: str | None,
        model: str,
        max_tokens: int | None,
        reasoning_effort: str | None = None,
    ) -> LLMTextResult:
        payload = _ollama_chat_payload(
            prompt,
            system_prompt=system_prompt,
            model=model,
            max_tokens=max_tokens,
        )
        data = await self._post_chat(payload, timeout=300.0)
        return LLMTextResult(text=_ollama_text(data), usage=_ollama_usage(data))

    async def generate_json(
        self,
        prompt: str,
        *,
        system_prompt: str | None,
        model: str,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        payload = _ollama_chat_payload(
            prompt,
            system_prompt=system_prompt
            or "You are a helpful assistant. Respond with valid JSON only.",
            model=model,
            max_tokens=max_tokens if max_tokens is not None else 8000,
        )
        payload["format"] = "json"
        data = await self._post_chat(payload, timeout=300.0)
        return _parse_json_response(_ollama_text(data))

    async def describe_image(
        self,
        image_path: str,
        *,
        context: str | None,
        model: str,
    ) -> str:
        try:
            _, _, encoded, _ = await prepare_image_data(image_path, logger)
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": _image_prompt(context),
                        "images": [encoded],
                    }
                ],
                "stream": False,
                "keep_alive": -1,
                "options": {"num_predict": 1024},
            }
            result = _ollama_text(await self._post_chat(payload, timeout=300.0))
            return validate_vision_description(result)
        except (VisionInputError, VisionProviderError):
            raise
        except Exception as exc:
            logger.error("Failed to describe image with Ollama: %s", exc)
            raise VisionProviderError(f"Ollama image description failed: {exc}") from exc

    async def _post_chat(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._origin}/api/chat",
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Ollama returned a non-object response")
        return data


def _image_prompt(context: str | None) -> str:
    prompt = (
        "Please describe this image in detail, focusing on key visual elements, "
        "any text visible, and the overall context or meaning."
    )
    if context:
        return f"{context}\n\n{prompt}"
    return prompt


def _lmstudio_text(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if isinstance(output, list):
        parts = [
            str(item.get("content") or "")
            for item in output
            if isinstance(item, dict) and item.get("type") == "message"
        ]
        return "".join(parts)

    for key in ("content", "text", "response"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _ollama_chat_payload(
    prompt: str,
    *,
    system_prompt: str | None,
    model: str,
    max_tokens: int | None,
) -> dict[str, Any]:
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    else:
        messages.append({"role": "system", "content": "You are a helpful assistant."})
    messages.append({"role": "user", "content": prompt})

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": -1,
    }
    if max_tokens:
        payload["options"] = {"num_predict": max_tokens}
    return payload


def _ollama_text(payload: dict[str, Any]) -> str:
    message = payload.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content

    response = payload.get("response")
    if isinstance(response, str):
        return response
    return ""


__all__ = [
    "LMStudioLocalProviderAdapter",
    "LocalProviderAdapter",
    "OllamaLocalProviderAdapter",
    "OpenAICompatibleLocalProviderAdapter",
    "create_local_provider_adapter",
]
