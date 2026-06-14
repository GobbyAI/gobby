"""Text generation service and candidate routing."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from gobby.ai._text_generation_contracts import (
    TextGenerateAdapter,
    TextGenerateAdapterFactory,
    TextGenerationRequest,
)
from gobby.ai._text_generation_helpers import (
    FeatureGenerationUnavailableError,
    _candidate_debug_label,
    _CandidateTimeoutError,
    _coerce_text_result,
    _elapsed_ms,
    _json_request,
    _parse_candidate,
    _parse_json_text,
    _validate_text_generation_output,
)
from gobby.ai.registry import (
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    CapabilityUnavailableError,
)
from gobby.config.feature_base import (
    FeatureProfile,
    default_candidates_for_profile,
    normalize_feature_candidate,
)
from gobby.llm.base import LLMProviderCancellation

if TYPE_CHECKING:
    from gobby.llm.base import LLMTextResult

logger = logging.getLogger("gobby.ai.text_generation")

# Lanes that spawn a cold subprocess (a CLI, the daemon transport, an ACP client,
# or the Claude SDK that itself spawns the claude CLI) pay cold-start latency and
# get the larger cli_candidate_timeout. Fast HTTP API lanes (local / OpenAI-
# compatible) keep the tight candidate_timeout.
_SPAWN_COLD_ADAPTER_STYLES: frozenset[AIAdapterStyle] = frozenset(
    {
        AIAdapterStyle.CLI,
        AIAdapterStyle.DAEMON,
        AIAdapterStyle.LLM_PROVIDER,
        AIAdapterStyle.ACP,
    }
)


def _json_parse_failure(raw: str, exc: Exception) -> ValueError:
    preview = raw[:240].replace("\n", "\\n").replace("\r", "\\r")
    if not preview:
        preview = "<empty>"
    return ValueError(
        f"Generated JSON parse failed: {type(exc).__name__}: {exc}; "
        f"raw_len={len(raw)}; raw_preview={preview!r}"
    )


class TextGenerationService:
    """Select and execute daemon text_generate capability bindings."""

    def __init__(
        self,
        registry: AICapabilityRegistry,
        adapters: Mapping[str, TextGenerateAdapter] | None = None,
        adapter_factories: Mapping[str, TextGenerateAdapterFactory] | None = None,
        profile_defaults: Mapping[FeatureProfile, Sequence[str]] | None = None,
        *,
        candidate_timeout_seconds: float | None = None,
        cli_candidate_timeout_seconds: float | None = None,
    ) -> None:
        self._registry = registry
        self._adapters = dict(adapters or {})
        self._adapter_factories = dict(adapter_factories or {})
        self._candidate_timeout_seconds = candidate_timeout_seconds
        self._cli_candidate_timeout_seconds = cli_candidate_timeout_seconds
        self._profile_defaults = {
            FeatureProfile(profile): tuple(
                normalize_feature_candidate(candidate) for candidate in candidates
            )
            for profile, candidates in (profile_defaults or {}).items()
        }

    def _candidate_timeout_for_binding(self, binding: CapabilityBinding | None) -> float | None:
        """Select the per-candidate timeout for the lane behind ``binding``.

        Spawn-cold lanes get ``cli_candidate_timeout_seconds`` (more headroom for
        cold-start); fast API lanes keep the tight ``candidate_timeout_seconds``.
        """
        if (
            binding is not None
            and binding.adapter_style in _SPAWN_COLD_ADAPTER_STYLES
            and self._cli_candidate_timeout_seconds is not None
        ):
            return self._cli_candidate_timeout_seconds
        return self._candidate_timeout_seconds

    async def _await_candidate[T](
        self, awaitable: Awaitable[T], *, binding: CapabilityBinding | None
    ) -> T:
        """Bound one candidate attempt by the lane-appropriate per-candidate timeout."""
        timeout = self._candidate_timeout_for_binding(binding)
        if timeout is None:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except TimeoutError as exc:
            raise _CandidateTimeoutError(f"candidate timed out after {timeout:g}s") from exc

    @property
    def registry(self) -> AICapabilityRegistry:
        """Return the capability registry used for selection."""
        return self._registry

    async def generate(self, request: TextGenerationRequest) -> str:
        """Select a text_generate binding and invoke its adapter."""
        return (await self.generate_result(request)).text

    async def generate_result(self, request: TextGenerationRequest) -> LLMTextResult:
        """Select a text_generate binding and invoke its adapter with usage."""
        candidates = self._candidate_requests(request)
        attempted_candidates: list[str] = []
        candidate_errors: dict[str, str] = {}
        candidate_unavailable_errors: list[CapabilityUnavailableError] = []
        text_result, last_error = await self._try_generate_result_candidates(
            candidates,
            attempted_candidates=attempted_candidates,
            candidate_errors=candidate_errors,
            candidate_unavailable_errors=candidate_unavailable_errors,
        )
        if text_result is not None:
            return text_result

        if len(attempted_candidates) == 1 and last_error is not None:
            raise last_error
        if unavailable_error := self._aggregate_unavailable_candidates_error(
            attempted_candidates=attempted_candidates,
            candidate_errors=candidate_errors,
            candidate_unavailable_errors=candidate_unavailable_errors,
            operation="text generation",
        ):
            raise unavailable_error from last_error
        raise FeatureGenerationUnavailableError(
            "No text generation candidate succeeded "
            f"(tried: {attempted_candidates}; errors: {candidate_errors})"
        ) from last_error

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        """Select a text_generate binding and return structured JSON."""
        candidates = self._candidate_requests(request)
        attempted_candidates: list[str] = []
        candidate_errors: dict[str, str] = {}
        candidate_unavailable_errors: list[CapabilityUnavailableError] = []
        result, last_error = await self._try_generate_json_candidates(
            candidates,
            attempted_candidates=attempted_candidates,
            candidate_errors=candidate_errors,
            candidate_unavailable_errors=candidate_unavailable_errors,
        )
        if result is not None:
            return result

        if len(attempted_candidates) == 1 and last_error is not None:
            raise last_error
        if unavailable_error := self._aggregate_unavailable_candidates_error(
            attempted_candidates=attempted_candidates,
            candidate_errors=candidate_errors,
            candidate_unavailable_errors=candidate_unavailable_errors,
            operation="JSON generation",
        ):
            raise unavailable_error from last_error
        raise FeatureGenerationUnavailableError(
            "No JSON generation candidate succeeded; "
            f"attempted candidates: {attempted_candidates}; errors: {candidate_errors}"
        ) from last_error

    async def _try_generate_result_candidates(
        self,
        candidates: tuple[TextGenerationRequest, ...],
        *,
        attempted_candidates: list[str],
        candidate_errors: dict[str, str],
        candidate_unavailable_errors: list[CapabilityUnavailableError],
    ) -> tuple[LLMTextResult | None, Exception | None]:
        last_error: Exception | None = None
        for index, candidate in enumerate(candidates):
            candidate_label = _candidate_debug_label(candidate)
            has_remaining_candidates = index < len(candidates) - 1
            attempted_candidates.append(candidate_label)
            start = time.perf_counter()
            binding: CapabilityBinding | None = None
            try:
                binding = self._select_binding(candidate)
                adapter = self._adapter_for_provider(binding.provider)
                result = await self._await_candidate(adapter.generate(candidate), binding=binding)
                text_result = _coerce_text_result(result)
                _validate_text_generation_output(candidate, text_result.text)
                self._log_generation_event(
                    request=candidate,
                    binding=binding,
                    latency_ms=_elapsed_ms(start),
                    success=True,
                )
                return (
                    replace(
                        text_result,
                        provider=binding.provider,
                        model=candidate.model or next(iter(binding.models), None),
                        profile=candidate.profile,
                    ),
                    None,
                )
            except LLMProviderCancellation:
                raise
            except Exception as exc:
                last_error = exc
                candidate_errors[candidate_label] = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, CapabilityUnavailableError):
                    candidate_unavailable_errors.append(exc)
                self._log_generation_event(
                    request=candidate,
                    binding=binding,
                    latency_ms=_elapsed_ms(start),
                    success=False,
                    error=exc,
                    terminal_failure=not has_remaining_candidates,
                )
                continue
        return None, last_error

    async def _try_generate_json_candidates(
        self,
        candidates: tuple[TextGenerationRequest, ...],
        *,
        attempted_candidates: list[str],
        candidate_errors: dict[str, str],
        candidate_unavailable_errors: list[CapabilityUnavailableError],
    ) -> tuple[dict[str, Any] | None, Exception | None]:
        last_error: Exception | None = None
        for index, candidate in enumerate(candidates):
            candidate_label = _candidate_debug_label(candidate)
            has_remaining_candidates = index < len(candidates) - 1
            attempted_candidates.append(candidate_label)
            start = time.perf_counter()
            binding: CapabilityBinding | None = None
            parse_outcome = "not_attempted"
            try:
                binding = self._select_binding(candidate)
                adapter = self._adapter_for_provider(binding.provider)
                json_adapter = getattr(adapter, "generate_json", None)
                if callable(json_adapter):
                    typed_json_adapter = cast(
                        Callable[[TextGenerationRequest], Awaitable[dict[str, Any]]],
                        json_adapter,
                    )
                    result = await self._await_candidate(
                        typed_json_adapter(candidate), binding=binding
                    )
                    parse_outcome = "provider_structured"
                else:
                    text = await self._await_candidate(
                        adapter.generate(_json_request(candidate)), binding=binding
                    )
                    raw = _coerce_text_result(text).text
                    try:
                        result = _parse_json_text(raw)
                    except (ValueError, json.JSONDecodeError) as exc:
                        raise _json_parse_failure(raw, exc) from exc
                    parse_outcome = "parsed_text"
                self._log_generation_event(
                    request=candidate,
                    binding=binding,
                    latency_ms=_elapsed_ms(start),
                    success=True,
                    json_parse_outcome=parse_outcome,
                )
                return result, None
            except LLMProviderCancellation:
                raise
            except Exception as exc:
                last_error = exc
                candidate_errors[candidate_label] = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, CapabilityUnavailableError):
                    candidate_unavailable_errors.append(exc)
                if parse_outcome == "not_attempted" and isinstance(
                    exc, (ValueError, json.JSONDecodeError)
                ):
                    parse_outcome = "parse_failed"
                self._log_generation_event(
                    request=candidate,
                    binding=binding,
                    latency_ms=_elapsed_ms(start),
                    success=False,
                    error=exc,
                    json_parse_outcome=parse_outcome,
                    terminal_failure=not has_remaining_candidates,
                )
        return None, last_error

    @staticmethod
    def _aggregate_unavailable_candidates_error(
        *,
        attempted_candidates: list[str],
        candidate_errors: dict[str, str],
        candidate_unavailable_errors: list[CapabilityUnavailableError],
        operation: str,
    ) -> CapabilityUnavailableError | None:
        if not attempted_candidates or len(candidate_unavailable_errors) != len(
            attempted_candidates
        ):
            return None

        details = "; ".join(
            f"{candidate}: {candidate_errors[candidate]}"
            for candidate in attempted_candidates
            if candidate in candidate_errors
        )
        return CapabilityUnavailableError(
            AICapability.TEXT_GENERATE,
            reason=f"All {operation} candidates unavailable: {details}",
        )

    def _candidate_requests(
        self, request: TextGenerationRequest
    ) -> tuple[TextGenerationRequest, ...]:
        if request.candidates:
            return tuple(
                replace(request, provider=provider, model=model)
                for provider, model in (
                    _parse_candidate(normalize_feature_candidate(candidate))
                    for candidate in request.candidates
                )
            )
        has_provider = request.provider is not None
        has_model = request.model is not None
        if has_provider != has_model:
            raise ValueError(
                "provider and model must be supplied together for explicit text generation routing"
            )
        if has_provider and has_model:
            return (request,)
        if request.profile:
            profile = FeatureProfile(request.profile)
            candidates = self._profile_defaults.get(profile)
            if candidates is None:
                candidates = default_candidates_for_profile(profile)
            return tuple(
                replace(request, provider=provider, model=model)
                for provider, model in (
                    _parse_candidate(normalize_feature_candidate(candidate))
                    for candidate in candidates
                )
            )
        return (request,)

    def _select_binding(self, request: TextGenerationRequest) -> CapabilityBinding:
        return self._registry.select(
            AICapability.TEXT_GENERATE,
            provider=request.provider,
            model=request.model,
        )

    def _adapter_for_provider(self, provider: str) -> TextGenerateAdapter:
        adapter = self._adapters.get(provider)
        if adapter is not None:
            return adapter

        factory = self._adapter_factories.get(provider)
        if factory is None:
            raise RuntimeError(f"No text_generate adapter registered for provider {provider!r}")
        try:
            adapter = factory()
        except Exception as exc:
            raise RuntimeError(
                f"Failed to initialize text_generate adapter for provider {provider!r}"
            ) from exc
        if adapter is None:
            raise RuntimeError(
                f"text_generate adapter factory for provider {provider!r} returned None"
            )
        self._adapters[provider] = adapter
        return adapter

    def _log_generation_event(
        self,
        *,
        request: TextGenerationRequest,
        binding: CapabilityBinding | None,
        latency_ms: float,
        success: bool,
        error: Exception | None = None,
        json_parse_outcome: str | None = None,
        terminal_failure: bool = False,
    ) -> None:
        provider = binding.provider if binding else request.provider
        model = request.model or (next(iter(binding.models), None) if binding else None)
        if success:
            log_event = logger.debug
        elif terminal_failure:
            log_event = logger.error
        else:
            log_event = logger.warning
        log_event(
            "feature_llm_call",
            extra={
                "feature": request.caller,
                "profile": request.profile,
                "provider": provider,
                "model": model,
                "latency_ms": round(latency_ms, 2),
                "success": success,
                "error": str(error) if error else None,
                "json_parse_outcome": json_parse_outcome,
            },
        )
