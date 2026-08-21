"""Text generation service and candidate routing."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from gobby.agents.provider_capabilities import provider_reasoning_flag
from gobby.agents.reasoning import normalize_reasoning_effort
from gobby.ai._agy_models import normalize_agy_model_selection, resolve_agy_effort
from gobby.ai._image_routing import (
    LocalModalityResolver,
    image_admission_diagnostic,
    image_candidate_eligible,
    image_transport_eligible,
)
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
from gobby.ai.endpoints import normalize_endpoint_routing
from gobby.ai.registry import (
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    CapabilityUnavailableError,
)
from gobby.config.feature_base import (
    DEFAULT_PROFILE_CANDIDATES,
    FeatureCandidateConfig,
    FeatureProfile,
    candidate_runtime_entries,
)
from gobby.llm.base import LLMProviderCancellation
from gobby.providers.capabilities.resolve import ReasoningStatus

if TYPE_CHECKING:
    from gobby.llm.base import LLMTextResult

logger = logging.getLogger("gobby.ai.text_generation")

# Lanes that spawn a cold subprocess (a CLI, the daemon transport, an ACP client,
# or the Claude SDK that itself spawns the claude CLI) pay cold-start latency and
# get the larger cli_candidate_timeout. Fast HTTP API lanes (local / OpenAI-
# compatible) keep the tight candidate_timeout.
__all__ = [
    "TextGenerationService",
    "image_candidate_eligible",
    "image_transport_eligible",
]

_SPAWN_COLD_ADAPTER_STYLES: frozenset[AIAdapterStyle] = frozenset(
    {
        AIAdapterStyle.CLI,
        AIAdapterStyle.DAEMON,
        AIAdapterStyle.LLM_PROVIDER,
        AIAdapterStyle.ACP,
    }
)

# Circuit breaker for the shared feature-LLM route. Under a sustained provider
# outage, every feature call would otherwise run the full slow try-all-candidates
# loop before failing; high-volume callers then compound that latency into a
# prolonged backlog (gobby-#17696). The breaker trips a
# provider binding after N consecutive failures and short-circuits further calls
# to it for a cooldown, so callers fail fast (and fall back) instead of hanging.
# It is a no-op in healthy operation: a single success resets the counter, and the
# open state is purely time-based so it can never latch permanently.
_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 8
_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 60.0
_CIRCUIT_BREAKER_MAX_KEYS = 256


class _CircuitOpenError(RuntimeError):
    """Raised to short-circuit a candidate whose provider binding is in cooldown."""

    def __init__(self, breaker_key: str, retry_after_seconds: float) -> None:
        self.breaker_key = breaker_key
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"circuit open for '{breaker_key}'; retry in {retry_after_seconds:.1f}s")


class _ReasoningEffortRejectedError(ValueError):
    """Raised when a candidate's reasoning effort must fail before adapter execution."""


def _all_candidates_rejected_reasoning(
    attempted_candidates: list[str],
    candidate_errors: list[tuple[str, str]],
) -> bool:
    return (
        bool(attempted_candidates)
        and len(candidate_errors) == len(attempted_candidates)
        and all(error.startswith("_ReasoningEffortRejectedError:") for _, error in candidate_errors)
    )


def _retry_after_from_exception(exc: BaseException) -> float | None:
    """Return a positive provider-reported cooldown carried on ``exc``, if any.

    Typed provider failures (e.g. a Claude rate-limit) expose ``retry_after`` in
    seconds so the breaker can open for the reported reset window. Read it
    structurally without importing provider-specific exception types.
    """
    retry_after = getattr(exc, "retry_after", None)
    if isinstance(retry_after, int | float) and not isinstance(retry_after, bool):
        return float(retry_after) if retry_after > 0.0 else None
    return None


def _json_parse_failure(raw: str, exc: Exception) -> ValueError:
    preview = raw[:240].replace("\n", "\\n").replace("\r", "\\r")
    if not preview:
        preview = "<empty>"
    return ValueError(
        f"Generated JSON parse failed: {type(exc).__name__}: {exc}; "
        f"raw_len={len(raw)}; raw_preview={preview!r}"
    )


def _gate_reasoning_effort(
    request: TextGenerationRequest,
    *,
    binding: CapabilityBinding,
) -> TextGenerationRequest:
    if binding.provider == "agy":
        request = _normalize_agy_request(request)
        # Resolve effort only against an explicit model. The adapter composes
        # --model solely from request.model (no binding fallback), so resolving
        # against binding.models[0] here would stamp an effort for a model the
        # adapter never sends. strict_models keeps request.model populated at
        # selection; this guard just keeps gate and adapter in lockstep.
        if request.model is None:
            return request
        try:
            resolved = resolve_agy_effort(request.model, request.reasoning_effort)
        except ValueError as exc:
            raise _ReasoningEffortRejectedError(str(exc)) from exc
        if request.reasoning_effort == resolved:
            return request
        return replace(request, reasoning_effort=resolved)

    normalized = normalize_reasoning_effort(request.reasoning_effort)
    if normalized is None:
        if request.reasoning_effort is None:
            return request
        return replace(request, reasoning_effort=None)

    execution_provider = binding.metadata.get("execution_provider")
    reasoning_provider = (
        execution_provider
        if isinstance(execution_provider, str) and execution_provider
        else binding.provider
    )
    transport_supports_effort = provider_reasoning_flag(reasoning_provider) is not None
    if not transport_supports_effort:
        return replace(request, reasoning_effort=None)

    from gobby.app_context import get_app_context

    ctx = get_app_context()
    resolver = getattr(ctx, "provider_capability_resolver", None) if ctx else None
    if resolver is not None:
        model = request.model or next(iter(binding.models), "")
        resolution = resolver.resolve_reasoning(
            reasoning_provider,
            model,
            normalized,
            transport_supports_effort=True,
        )
        if resolution.status is ReasoningStatus.REJECTED:
            raise _ReasoningEffortRejectedError(
                f"Unsupported reasoning_effort {normalized!r} for provider "
                f"{binding.provider!r}: {resolution.reason}"
            )
        normalized = resolution.effective_effort or normalized

    if normalized == request.reasoning_effort:
        return request
    return replace(request, reasoning_effort=normalized)


def _request_has_images(request: TextGenerationRequest) -> bool:
    return bool(request.images)


def _normalize_agy_request(request: TextGenerationRequest) -> TextGenerationRequest:
    if request.provider != "agy":
        return request
    model, reasoning_effort = normalize_agy_model_selection(
        request.model,
        request.reasoning_effort,
    )
    if model == request.model and reasoning_effort == request.reasoning_effort:
        return request
    return replace(request, model=model, reasoning_effort=reasoning_effort)


class TextGenerationService:
    """Select and execute daemon text_generate capability bindings."""

    def __init__(
        self,
        registry: AICapabilityRegistry,
        adapters: Mapping[str, TextGenerateAdapter] | None = None,
        adapter_factories: Mapping[str, TextGenerateAdapterFactory] | None = None,
        profile_defaults: Mapping[FeatureProfile, Sequence[str | FeatureCandidateConfig]]
        | None = None,
        *,
        candidate_timeout_seconds: float | None = None,
        cli_candidate_timeout_seconds: float | None = None,
        spawn_cold_max_concurrency: int = 3,
        circuit_breaker_failure_threshold: int = _CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        circuit_breaker_cooldown_seconds: float = _CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        local_modality_resolver: LocalModalityResolver | None = None,
    ) -> None:
        if spawn_cold_max_concurrency < 1:
            raise ValueError("spawn_cold_max_concurrency must be >= 1")
        self._registry = registry
        self._local_modality_resolver = local_modality_resolver
        self._adapters = dict(adapters or {})
        self._adapter_factories = dict(adapter_factories or {})
        self._candidate_timeout_seconds = candidate_timeout_seconds
        self._cli_candidate_timeout_seconds = cli_candidate_timeout_seconds
        self._spawn_cold_max_concurrency = spawn_cold_max_concurrency
        self._spawn_cold_gate = asyncio.Semaphore(spawn_cold_max_concurrency)
        self._profile_defaults = {
            FeatureProfile(profile): candidate_runtime_entries(candidates, profile=profile)
            for profile, candidates in (profile_defaults or {}).items()
        }
        self._circuit_breaker_failure_threshold = circuit_breaker_failure_threshold
        self._circuit_breaker_cooldown_seconds = circuit_breaker_cooldown_seconds
        self._breaker_failures: dict[str, int] = {}
        self._breaker_open_until: dict[str, float] = {}

    @staticmethod
    def _breaker_key(binding: CapabilityBinding, candidate: TextGenerationRequest) -> str:
        model = candidate.model or next(iter(binding.models), "")
        return f"{binding.provider}:{model}"

    def _breaker_retry_after(self, key: str) -> float:
        """Seconds until the breaker for key reopens, or 0.0 when it is closed."""
        if self._circuit_breaker_failure_threshold <= 0:
            return 0.0
        self._prune_breaker_state()
        remaining = self._breaker_open_until.get(key, 0.0) - time.monotonic()
        return remaining if remaining > 0.0 else 0.0

    def _breaker_record_success(self, key: str) -> None:
        self._prune_breaker_state()
        if key in self._breaker_open_until or key in self._breaker_failures:
            logger.info("circuit closed for '%s' (probe succeeded)", key)
        self._breaker_failures.pop(key, None)
        self._breaker_open_until.pop(key, None)
        self._prune_breaker_state()

    def _breaker_record_failure(self, key: str, *, retry_after: float | None = None) -> None:
        if self._circuit_breaker_failure_threshold <= 0:
            return
        self._prune_breaker_state()
        was_open = self._breaker_open_until.get(key, 0.0) > time.monotonic()
        if retry_after is not None and retry_after > 0.0:
            # A provider-reported cooldown (e.g. a rate-limit reset window) is
            # definitive: open the breaker immediately for the reported window
            # instead of waiting for the consecutive-failure threshold, and never
            # shorten an already-longer cooldown.
            self._breaker_open_until[key] = max(
                self._breaker_open_until.get(key, 0.0),
                time.monotonic() + retry_after,
            )
            if not was_open:
                logger.info(
                    "circuit open for '%s' (provider-reported retry-after %.1fs)",
                    key,
                    retry_after,
                )
            self._prune_breaker_state()
            return
        failures = self._breaker_failures.get(key, 0) + 1
        self._breaker_failures[key] = failures
        if failures >= self._circuit_breaker_failure_threshold:
            self._breaker_open_until[key] = (
                time.monotonic() + self._circuit_breaker_cooldown_seconds
            )
            if not was_open:
                logger.info(
                    "circuit open for '%s' after %d consecutive failures",
                    key,
                    failures,
                )
        self._prune_breaker_state()

    def _prune_breaker_state(self) -> None:
        now = time.monotonic()
        expired = [key for key, open_until in self._breaker_open_until.items() if open_until <= now]
        for key in expired:
            self._breaker_open_until.pop(key, None)

        keys = list(dict.fromkeys([*self._breaker_failures, *self._breaker_open_until]))
        overflow = len(keys) - _CIRCUIT_BREAKER_MAX_KEYS
        if overflow <= 0:
            return
        for key in keys[:overflow]:
            self._breaker_failures.pop(key, None)
            self._breaker_open_until.pop(key, None)

    def _candidate_timeout_for_binding(
        self, request: TextGenerationRequest, binding: CapabilityBinding | None
    ) -> float | None:
        """Select the per-candidate timeout for the lane behind ``binding``.

        Spawn-cold lanes get ``cli_candidate_timeout_seconds`` (more headroom for
        cold-start); fast API lanes keep the tight ``candidate_timeout_seconds``.
        """
        if binding is not None and binding.adapter_style in _SPAWN_COLD_ADAPTER_STYLES:
            return (
                request.cli_candidate_timeout_seconds
                if request.cli_candidate_timeout_seconds is not None
                else self._cli_candidate_timeout_seconds
            )
        return (
            request.candidate_timeout_seconds
            if request.candidate_timeout_seconds is not None
            else self._candidate_timeout_seconds
        )

    async def _await_candidate[T](
        self,
        awaitable: Awaitable[T],
        *,
        request: TextGenerationRequest,
        binding: CapabilityBinding | None,
    ) -> T:
        """Bound one candidate attempt by the lane-appropriate per-candidate timeout."""
        timeout = self._candidate_timeout_for_binding(request, binding)
        if timeout is None:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except TimeoutError as exc:
            raise _CandidateTimeoutError(f"candidate timed out after {timeout:g}s") from exc

    async def _await_admitted_candidate[T](
        self,
        awaitable_factory: Callable[[], Awaitable[T]],
        *,
        request: TextGenerationRequest,
        binding: CapabilityBinding,
    ) -> T:
        if binding.adapter_style not in _SPAWN_COLD_ADAPTER_STYLES:
            return await self._await_candidate(
                awaitable_factory(), request=request, binding=binding
            )

        async with self._spawn_cold_gate:
            return await self._await_candidate(
                awaitable_factory(), request=request, binding=binding
            )

    @property
    def registry(self) -> AICapabilityRegistry:
        """Return the capability registry used for selection."""
        return self._registry

    async def generate(self, request: TextGenerationRequest) -> str:
        """Select a text_generate binding and invoke its adapter."""
        return (await self.generate_result(request)).text

    async def generate_result(self, request: TextGenerationRequest) -> LLMTextResult:
        """Select a text_generate binding and invoke its adapter with usage."""
        if request.total_timeout_seconds is None:
            return await self._generate_result(request)
        try:
            return await asyncio.wait_for(
                self._generate_result(request),
                timeout=request.total_timeout_seconds,
            )
        except TimeoutError as exc:
            raise FeatureGenerationUnavailableError(
                f"Text generation exceeded total timeout ({request.total_timeout_seconds:g}s)"
            ) from exc

    async def _generate_result(self, request: TextGenerationRequest) -> LLMTextResult:
        candidates = await self._admitted_candidates(request)
        attempted_candidates: list[str] = []
        candidate_errors: list[tuple[str, str]] = []
        candidate_unavailable_errors: list[CapabilityUnavailableError] = []
        text_result, last_error = await self._try_generate_result_candidates(
            candidates,
            attempted_candidates=attempted_candidates,
            candidate_errors=candidate_errors,
            candidate_unavailable_errors=candidate_unavailable_errors,
        )
        if text_result is not None:
            return text_result

        if (
            last_error is not None
            and isinstance(last_error, _ReasoningEffortRejectedError)
            and _all_candidates_rejected_reasoning(attempted_candidates, candidate_errors)
        ):
            raise last_error
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
        if request.total_timeout_seconds is None:
            return await self._generate_json(request)
        try:
            return await asyncio.wait_for(
                self._generate_json(request),
                timeout=request.total_timeout_seconds,
            )
        except TimeoutError as exc:
            raise FeatureGenerationUnavailableError(
                f"JSON generation exceeded total timeout ({request.total_timeout_seconds:g}s)"
            ) from exc

    async def _generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        candidates = await self._admitted_candidates(request)
        attempted_candidates: list[str] = []
        candidate_errors: list[tuple[str, str]] = []
        candidate_unavailable_errors: list[CapabilityUnavailableError] = []
        result, last_error = await self._try_generate_json_candidates(
            candidates,
            attempted_candidates=attempted_candidates,
            candidate_errors=candidate_errors,
            candidate_unavailable_errors=candidate_unavailable_errors,
        )
        if result is not None:
            return result

        if (
            last_error is not None
            and isinstance(last_error, _ReasoningEffortRejectedError)
            and _all_candidates_rejected_reasoning(attempted_candidates, candidate_errors)
        ):
            raise last_error
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
        candidate_errors: list[tuple[str, str]],
        candidate_unavailable_errors: list[CapabilityUnavailableError],
    ) -> tuple[LLMTextResult | None, Exception | None]:
        last_error: Exception | None = None
        last_reasoning_error: _ReasoningEffortRejectedError | None = None
        reasoning_rejections = 0
        for index, candidate in enumerate(candidates):
            candidate = _normalize_agy_request(candidate)
            candidate_label = _candidate_debug_label(candidate)
            has_remaining_candidates = index < len(candidates) - 1
            attempted_candidates.append(candidate_label)
            start = time.perf_counter()
            binding: CapabilityBinding | None = None
            breaker_key: str | None = None
            try:
                binding = self._select_binding(candidate)
                breaker_key = self._breaker_key(binding, candidate)
                retry_after = self._breaker_retry_after(breaker_key)
                if retry_after > 0.0:
                    raise _CircuitOpenError(breaker_key, retry_after)
                candidate = _gate_reasoning_effort(candidate, binding=binding)
                adapter = self._adapter_for_provider(binding.provider)

                def generate_candidate(
                    adapter: TextGenerateAdapter = adapter,
                    candidate: TextGenerationRequest = candidate,
                ) -> Awaitable[str | LLMTextResult]:
                    return adapter.generate(candidate)

                try:
                    result = await self._await_admitted_candidate(
                        generate_candidate,
                        request=candidate,
                        binding=binding,
                    )
                except Exception as exc:
                    self._breaker_record_failure(
                        breaker_key, retry_after=_retry_after_from_exception(exc)
                    )
                    raise
                self._breaker_record_success(breaker_key)
                text_result = _coerce_text_result(
                    result,
                    applied_reasoning_effort=candidate.reasoning_effort,
                )
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
                        applied_reasoning_effort=text_result.applied_reasoning_effort,
                    ),
                    None,
                )
            except LLMProviderCancellation:
                raise
            except _CircuitOpenError as exc:
                last_error = exc
                candidate_errors.append((candidate_label, f"{type(exc).__name__}: {exc}"))
                self._log_generation_event(
                    request=candidate,
                    binding=binding,
                    latency_ms=_elapsed_ms(start),
                    success=False,
                    error=exc,
                    terminal_failure=not has_remaining_candidates,
                )
                continue
            except _ReasoningEffortRejectedError as exc:
                last_error = exc
                last_reasoning_error = exc
                reasoning_rejections += 1
                candidate_errors.append((candidate_label, f"{type(exc).__name__}: {exc}"))
                self._log_generation_event(
                    request=candidate,
                    binding=binding,
                    latency_ms=_elapsed_ms(start),
                    success=False,
                    error=exc,
                    terminal_failure=not has_remaining_candidates,
                )
                continue
            except Exception as exc:
                last_error = exc
                candidate_errors.append((candidate_label, f"{type(exc).__name__}: {exc}"))
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
        if (
            attempted_candidates
            and reasoning_rejections == len(attempted_candidates)
            and last_reasoning_error is not None
        ):
            return None, last_reasoning_error
        return None, last_error

    async def _try_generate_json_candidates(
        self,
        candidates: tuple[TextGenerationRequest, ...],
        *,
        attempted_candidates: list[str],
        candidate_errors: list[tuple[str, str]],
        candidate_unavailable_errors: list[CapabilityUnavailableError],
    ) -> tuple[dict[str, Any] | None, Exception | None]:
        last_error: Exception | None = None
        last_reasoning_error: _ReasoningEffortRejectedError | None = None
        reasoning_rejections = 0
        for index, candidate in enumerate(candidates):
            candidate = _normalize_agy_request(candidate)
            candidate_label = _candidate_debug_label(candidate)
            has_remaining_candidates = index < len(candidates) - 1
            attempted_candidates.append(candidate_label)
            start = time.perf_counter()
            binding: CapabilityBinding | None = None
            parse_outcome = "not_attempted"
            try:
                binding = self._select_binding(candidate)
                candidate = _gate_reasoning_effort(candidate, binding=binding)
                adapter = self._adapter_for_provider(binding.provider)
                json_adapter = getattr(adapter, "generate_json", None)
                if callable(json_adapter):
                    typed_json_adapter = cast(
                        Callable[[TextGenerationRequest], Awaitable[dict[str, Any]]],
                        json_adapter,
                    )

                    def generate_structured_json(
                        typed_json_adapter: Callable[
                            [TextGenerationRequest], Awaitable[dict[str, Any]]
                        ] = typed_json_adapter,
                        candidate: TextGenerationRequest = candidate,
                    ) -> Awaitable[dict[str, Any]]:
                        return typed_json_adapter(candidate)

                    result = await self._await_admitted_candidate(
                        generate_structured_json,
                        request=candidate,
                        binding=binding,
                    )
                    parse_outcome = "provider_structured"
                else:
                    json_text_request = _json_request(candidate)

                    def generate_json_text(
                        adapter: TextGenerateAdapter = adapter,
                        json_text_request: TextGenerationRequest = json_text_request,
                    ) -> Awaitable[str | LLMTextResult]:
                        return adapter.generate(json_text_request)

                    text = await self._await_admitted_candidate(
                        generate_json_text,
                        request=json_text_request,
                        binding=binding,
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
            except _ReasoningEffortRejectedError as exc:
                last_error = exc
                last_reasoning_error = exc
                reasoning_rejections += 1
                candidate_errors.append((candidate_label, f"{type(exc).__name__}: {exc}"))
                self._log_generation_event(
                    request=candidate,
                    binding=binding,
                    latency_ms=_elapsed_ms(start),
                    success=False,
                    error=exc,
                    json_parse_outcome=parse_outcome,
                    terminal_failure=not has_remaining_candidates,
                )
                continue
            except Exception as exc:
                last_error = exc
                candidate_errors.append((candidate_label, f"{type(exc).__name__}: {exc}"))
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
        if (
            attempted_candidates
            and reasoning_rejections == len(attempted_candidates)
            and last_reasoning_error is not None
        ):
            return None, last_reasoning_error
        return None, last_error

    @staticmethod
    def _aggregate_unavailable_candidates_error(
        *,
        attempted_candidates: list[str],
        candidate_errors: list[tuple[str, str]],
        candidate_unavailable_errors: list[CapabilityUnavailableError],
        operation: str,
    ) -> CapabilityUnavailableError | None:
        if not attempted_candidates or len(candidate_unavailable_errors) != len(
            attempted_candidates
        ):
            return None

        details = "; ".join(f"{candidate}: {error}" for candidate, error in candidate_errors)
        return CapabilityUnavailableError(
            AICapability.TEXT_GENERATE,
            reason=f"All {operation} candidates unavailable: {details}",
        )

    async def _admitted_candidates(
        self, request: TextGenerationRequest
    ) -> tuple[TextGenerationRequest, ...]:
        candidates = self._candidate_requests(request)
        if not _request_has_images(request):
            return candidates
        eligible: list[TextGenerationRequest] = []
        diagnostics: list[str] = []
        selection_errors: list[Exception] = []
        for candidate in candidates:
            try:
                binding = self._select_binding(candidate)
            except (CapabilityUnavailableError, ValueError) as exc:
                selection_errors.append(exc)
                continue
            diagnostic = await image_admission_diagnostic(
                binding, candidate.model, self._local_modality_resolver
            )
            if diagnostic is None:
                eligible.append(candidate)
            else:
                diagnostics.append(diagnostic)
        if eligible:
            return tuple(eligible)
        if diagnostics:
            raise ValueError(diagnostics[0])
        if selection_errors:
            raise selection_errors[0]
        raise ValueError("Image inputs are not supported by the selected candidate")

    def _candidate_requests(
        self, request: TextGenerationRequest
    ) -> tuple[TextGenerationRequest, ...]:
        if request.candidates:
            return tuple(
                self._candidate_request(request, candidate)
                for candidate in candidate_runtime_entries(
                    request.candidates,
                    profile=request.profile,
                )
            )
        provider, model = normalize_endpoint_routing(request.provider, request.model)
        if provider != request.provider or model != request.model:
            request = replace(request, provider=provider, model=model)
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
                candidates = DEFAULT_PROFILE_CANDIDATES[profile]
            return tuple(
                self._candidate_request(request, candidate)
                for candidate in candidate_runtime_entries(candidates, profile=profile)
            )
        return (request,)

    @staticmethod
    def _candidate_request(
        request: TextGenerationRequest,
        candidate: FeatureCandidateConfig,
    ) -> TextGenerationRequest:
        provider, model = _parse_candidate(candidate.candidate)
        provider, model = normalize_endpoint_routing(provider, model)
        reasoning_effort = (
            request.reasoning_effort
            if request.reasoning_effort is not None
            else candidate.reasoning_effort
        )
        return replace(
            request,
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
        )

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
            message = "feature_llm_call"
        elif isinstance(error, _CircuitOpenError):
            log_event = logger.debug
            message = "feature_llm_call"
        elif isinstance(error, _ReasoningEffortRejectedError):
            log_event = logger.warning
            message = f"feature_llm_call: {error}"
        elif terminal_failure:
            log_event = logger.error
            message = "feature_llm_call"
        else:
            log_event = logger.debug
            message = "feature_llm_call"
        log_event(
            message,
            extra={
                "feature": request.caller,
                "profile": request.profile,
                "provider": provider,
                "model": model,
                "candidate": _candidate_debug_label(request),
                "latency_ms": round(latency_ms, 2),
                "success": success,
                "error": str(error) if error else None,
                "json_parse_outcome": json_parse_outcome,
                "reasoning_effort": request.reasoning_effort,
            },
        )
