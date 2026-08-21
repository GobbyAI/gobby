"""Local LLM provider implementation.

Routes LLM calls to a configured local endpoint provider.

Used when feature candidate routing selects an ``endpoint:<name>/<model>``
candidate, giving lightweight, zero-cost inference while preserving profile
fallback to the next configured candidate when the local server is down.
"""

import logging
from typing import Any

from gobby.ai.endpoints import (
    endpoint_provider,
    resolve_generation_endpoint,
)
from gobby.llm.base import AuthMode, LLMTextResult
from gobby.llm.local_provider_adapters import create_local_provider_adapter

logger = logging.getLogger(__name__)

# Known cloud model shortnames that shouldn't be sent to a local server.
_CLOUD_MODEL_ALIASES: frozenset[str] = frozenset(
    {
        # Claude
        "haiku",
        "sonnet",
        "opus",
        "fable",
        # OpenAI
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-5",
        "gpt-5-mini",
        "o1",
        "o3",
        "o3-mini",
        "o4-mini",
    }
)


class LocalLLMProvider:
    """LLM provider for configured local endpoints.

    Feature and non-feature local generation pass a named
    ``DaemonConfig.ai.generation.endpoints`` entry:
    - ``provider``: Backend adapter (``openai-compatible``, ``lmstudio``, ``ollama``)
    - ``api_base``: Base URL (e.g. ``http://localhost:1234``)
    - ``model``: Default model name to request
    - ``api_key``: Optional API key (some local servers require one)
    """

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def auth_mode(self) -> AuthMode:
        return "api_key"

    def __init__(self, config: Any, endpoint_name: str) -> None:
        """Initialise from DaemonConfig.

        Args:
            config: DaemonConfig instance with local endpoint config.
            endpoint_name: Named generation endpoint.

        Raises:
            ValueError: If no local endpoint is configured.
        """
        if not endpoint_name:
            raise ValueError("Local LLM provider requires a named local generation endpoint")
        local_cfg = resolve_generation_endpoint(config, endpoint_name)
        self._provider_name = endpoint_provider(endpoint_name)

        url = local_cfg.api_base
        model = local_cfg.model
        if not url or not model:
            raise ValueError("Local generation endpoint requires api_base and model")
        self._url = str(url)
        self._default_model = str(model)
        self._api_key = str(getattr(local_cfg, "api_key", None) or "not-needed")
        self._endpoint = local_cfg
        self._adapter = create_local_provider_adapter(local_cfg)
        self._client: Any | None = self._adapter.client
        logger.debug(
            "Local LLM provider initialised (provider=%s, url=%s, model=%s)",
            local_cfg.protocol,
            self._url,
            self._default_model,
        )

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    def _resolve_model(self, model: str | None) -> str:
        """Return a model name safe to send to the local endpoint.

        If *model* is a known cloud alias (e.g. ``"haiku"``), log a warning
        and fall back to the endpoint's configured model.
        """
        if model is None:
            return self._default_model

        if model.lower() in _CLOUD_MODEL_ALIASES:
            logger.warning(
                "Model '%s' is a cloud alias — using local default '%s' instead",
                model,
                self._default_model,
            )
            return self._default_model

        return model

    async def _resolve_wire_model(self, model: str | None) -> str:
        requested = self._resolve_model(model)
        if self._endpoint.protocol != "vllm":
            return requested
        from gobby.agents.local_model import resolve_vllm_served_model

        endpoint = self._endpoint
        if requested != endpoint.model:
            endpoint = endpoint.model_copy(update={"model": requested})
        return await resolve_vllm_served_model(endpoint)

    # ------------------------------------------------------------------
    # Provider primitives
    # ------------------------------------------------------------------

    async def generate_text(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        *,
        reasoning_effort: str | None = None,
        caller: str | None = None,
    ) -> str:
        return (
            await self.generate_text_result(
                prompt,
                system_prompt=system_prompt,
                model=model,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                caller=caller,
            )
        ).text

    async def generate_text_result(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        *,
        reasoning_effort: str | None = None,
        caller: str | None = None,
        images: list[str] | None = None,
    ) -> LLMTextResult:
        if caller:
            logger.debug("Local LLM text request from %s", caller)
        resolved = await self._resolve_wire_model(model)
        return await self._adapter.generate_text_result(
            prompt,
            system_prompt=system_prompt,
            model=resolved,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            images=images,
        )

    async def generate_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        *,
        reasoning_effort: str | None = None,
        caller: str | None = None,
    ) -> dict[str, Any]:
        if caller:
            logger.debug("Local LLM JSON request from %s", caller)
        resolved = await self._resolve_wire_model(model)
        return await self._adapter.generate_json(
            prompt,
            system_prompt=system_prompt,
            model=resolved,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
