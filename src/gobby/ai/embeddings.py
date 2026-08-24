"""Embedding generation via OpenAI-compatible APIs.

Supports local Ollama/LM Studio endpoints and OpenAI cloud models. The
``EmbeddingService`` wrapper exposes synchronous configuration checks,
asynchronous reachability probes, health checks, and cached generation.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from gobby.config.persistence import EmbeddingsConfig

logger = logging.getLogger(__name__)

_OPENAI_CLOUD_API_BASE = "https://api.openai.com/v1"
_OPENAI_CLOUD_MODEL_DIMS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


class EmbeddingGenerationError(RuntimeError):
    """Raised for expected provider-side embedding generation failures."""


@dataclass(frozen=True, slots=True)
class _ResolvedEmbeddingProvider:
    """Provider settings validated for one embedding request."""

    model: str
    api_base: str | None
    api_key: str


def _normalize_api_base(api_base: str | None) -> str | None:
    """Normalize empty strings to None while preserving configured endpoints."""
    if api_base is None:
        return None
    normalized = api_base.strip()
    return normalized or None


def _strip_openai_prefix(model: str) -> str:
    """Return the provider-native model name for explicit OpenAI-prefixed config."""
    if model.lower().startswith("openai/"):
        return model.split("/", 1)[1]
    return model


def _strip_local_embedding_prefix(model: str) -> str:
    for prefix in ("local:lm-studio/", "local:ollama/"):
        if model.startswith(prefix):
            return model.removeprefix(prefix)
    return model


def is_openai_cloud_embedding_model(model: str | None) -> bool:
    """Return True when model names a built-in OpenAI cloud embedding model."""
    if not model:
        return False
    return _strip_openai_prefix(model) in _OPENAI_CLOUD_MODEL_DIMS


def _masked_key_state(api_key: str | None) -> str:
    """Report key presence without leaking the key."""
    return "[set]" if api_key else "[not set]"


def _embedding_unavailable_message(
    *,
    model: str,
    api_base: str | None,
    api_key: str | None,
    reason: str,
) -> str:
    """Build a clear provider-unavailable error without exposing secrets."""
    return (
        "Embedding provider unavailable: "
        f"{reason} "
        f"(model={model}, api_base={api_base}, api_key={_masked_key_state(api_key)})"
    )


def _resolve_embedding_provider(
    *,
    model: str,
    api_base: str | None,
    api_key: str | None,
) -> _ResolvedEmbeddingProvider:
    """Resolve and validate the embedding provider before any SDK call.

    Local/OpenAI-compatible providers must set ``api_base``. OpenAI cloud is
    allowed only for a known OpenAI embedding model with an explicit API key.
    """
    normalized_api_base = _normalize_api_base(api_base)
    if normalized_api_base:
        return _ResolvedEmbeddingProvider(
            model=_strip_local_embedding_prefix(model),
            api_base=normalized_api_base,
            api_key=api_key or "unused",
        )

    if not is_openai_cloud_embedding_model(model):
        raise EmbeddingGenerationError(
            _embedding_unavailable_message(
                model=model,
                api_base=normalized_api_base,
                api_key=api_key,
                reason=(
                    "local or OpenAI-compatible embedding models require an embedding API base"
                ),
            )
        )

    if not api_key:
        raise EmbeddingGenerationError(
            _embedding_unavailable_message(
                model=model,
                api_base=normalized_api_base,
                api_key=api_key,
                reason="OpenAI cloud embeddings require an embedding API key",
            )
        )

    return _ResolvedEmbeddingProvider(
        model=_strip_openai_prefix(model),
        api_base=None,
        api_key=api_key,
    )


# Default retry settings for rate-limited requests
_DEFAULT_MAX_RETRIES = 5
_DEFAULT_BASE_DELAY = 1.0  # seconds
_DEFAULT_MAX_DELAY = 60.0  # seconds

# Cooldown for model-reload attempts (prevents hammering lms/ollama)
_RELOAD_COOLDOWN = 60.0  # seconds
_last_reload_attempt: float = 0.0

# Cooldown for local LM Studio service recovery after connection failures.
_LOCAL_LM_STUDIO_RECOVERY_COOLDOWN = 60.0  # seconds
_last_local_lm_studio_recovery_attempt: float = 0.0

# ---------------------------------------------------------------------------
# TTL cache for embedding results
# ---------------------------------------------------------------------------
_CACHE_TTL = 60.0  # seconds
_CACHE_MAX_SIZE = 2048


@dataclass(slots=True)
class _CacheEntry:
    embedding: list[float]
    expires_at: float


_cache: dict[str, _CacheEntry] = {}
_inflight: dict[str, asyncio.Future[list[float]]] = {}
# Initialize at module import: Python's import machinery is serialized, so two
# concurrent _get_lock() callers cannot race to create distinct RLock objects.
# The previous lazy-init pattern had exactly that race — two threads arriving
# with _cache_lock=None would each call RLock() and one would overwrite the
# other, leaving concurrent cache writers synchronized on different locks.
_cache_lock: RLock = RLock()


def _get_lock() -> RLock:
    """Return the shared cache lock. Preserved as a function for call-site stability."""
    return _cache_lock


def _cache_key(text: str, model: str, api_base: str | None) -> str:
    """Stable cache key from text content + model + endpoint."""
    h = hashlib.sha256(text.encode()).hexdigest()[:16]
    return f"{h}:{model}:{api_base or 'default'}"


def _evict_expired() -> None:
    """Remove expired entries. Called while holding the lock."""
    now = time.monotonic()
    expired = [k for k, v in _cache.items() if v.expires_at <= now]
    for k in expired:
        del _cache[k]


def _enforce_max_size() -> None:
    """Evict oldest entries if cache exceeds max size."""
    if len(_cache) <= _CACHE_MAX_SIZE:
        return
    # Sort by expiry (oldest first) and remove excess
    by_expiry = sorted(_cache.items(), key=lambda kv: kv[1].expires_at)
    to_remove = len(_cache) - _CACHE_MAX_SIZE
    for key, _ in by_expiry[:to_remove]:
        del _cache[key]


def _clear_embedding_cache() -> None:
    """Clear the embedding cache. Useful for testing."""
    with _get_lock():
        _cache.clear()
        for pending in _inflight.values():
            if not pending.done():
                pending.cancel()
        _inflight.clear()


def _consume_future_exception(future: asyncio.Future[list[float]]) -> None:
    if not future.cancelled():
        future.exception()


def _needs_nomic_prefix(model: str) -> bool:
    """Check if a model requires nomic-style task prefixes."""
    return "nomic" in model.lower()


def _apply_prefix(text: str, is_query: bool, model: str, query_prefix: str | None = None) -> str:
    """Prepend nomic task prefix when applicable.

    nomic-embed-text was trained with task-specific prefixes:
    - 'search_query: ' for queries
    - 'search_document: ' for documents
    """
    if is_query and query_prefix:
        return f"{query_prefix}{text}"
    if not _needs_nomic_prefix(model):
        return text
    if is_query:
        return f"search_query: {text}"
    return f"search_document: {text}"


async def _generate_embeddings(
    texts: list[str],
    model: str = "nomic-embed-text",
    api_base: str | None = None,
    api_key: str | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    base_delay: float = _DEFAULT_BASE_DELAY,
    is_query: bool = False,
    expected_dim: int | None = None,
    query_prefix: str | None = None,
) -> list[list[float]]:
    """Generate embeddings with cache and in-flight dedupe."""
    if not texts:
        return []

    prefixed_texts = [_apply_prefix(t, is_query, model, query_prefix) for t in texts]

    lock = _get_lock()
    loop = asyncio.get_running_loop()

    with lock:
        _evict_expired()
        results: list[list[float] | None] = []
        pending: list[tuple[int, str, asyncio.Future[list[float]]]] = []
        new_miss_keys: list[str] = []
        new_miss_texts: list[str] = []
        new_miss_futures: list[asyncio.Future[list[float]]] = []

        for i, text in enumerate(prefixed_texts):
            key = _cache_key(text, model, api_base)
            entry = _cache.get(key)
            if (
                entry is not None
                and expected_dim is not None
                and len(entry.embedding) != expected_dim
            ):
                del _cache[key]
                entry = None
            if entry is not None:
                results.append(entry.embedding)
            else:
                future = _inflight.get(key)
                if future is None:
                    future = loop.create_future()
                    _inflight[key] = future
                    new_miss_keys.append(key)
                    new_miss_texts.append(text)
                    new_miss_futures.append(future)
                results.append(None)
                pending.append((i, key, future))

    if new_miss_texts:
        try:
            fresh = await _fetch_embeddings(
                texts=new_miss_texts,
                model=model,
                api_base=api_base,
                api_key=api_key,
                max_retries=max_retries,
                base_delay=base_delay,
                expected_dim=expected_dim,
            )
        except Exception as exc:
            with lock:
                for key, future in zip(new_miss_keys, new_miss_futures, strict=True):
                    if _inflight.get(key) is future:
                        del _inflight[key]
                    if not future.done():
                        future.set_exception(exc)
                        future.add_done_callback(_consume_future_exception)
            raise
        with lock:
            now = time.monotonic()
            expires_at = now + _CACHE_TTL
            for key, emb, future in zip(new_miss_keys, fresh, new_miss_futures, strict=True):
                _cache[key] = _CacheEntry(embedding=emb, expires_at=expires_at)
                if _inflight.get(key) is future:
                    del _inflight[key]
                if not future.done():
                    future.set_result(emb)
            _enforce_max_size()

    for i, key, future in pending:
        embedding = await future
        if expected_dim is not None and len(embedding) != expected_dim:
            with lock:
                _cache.pop(key, None)
            raise EmbeddingGenerationError(
                f"Embedding dimension mismatch for model={model}: "
                f"expected {expected_dim}, got {len(embedding)}"
            )
        results[i] = embedding

    filled_results: list[list[float]] = []
    for result in results:
        if result is None:
            raise EmbeddingGenerationError("Embedding cache fill left a missing result")
        filled_results.append(result)
    return filled_results


async def _try_reload_model(model: str, api_base: str) -> bool:
    """Attempt to reload an evicted model on a local inference server.

    Respects a cooldown to avoid hammering the server when multiple
    concurrent calls all see the same eviction error.
    """
    global _last_reload_attempt
    now = time.monotonic()
    if now - _last_reload_attempt < _RELOAD_COOLDOWN:
        logger.debug("Skipping model reload — cooldown active")
        return False
    _last_reload_attempt = now

    from gobby.cli.services import try_autoload_embedding_model

    logger.info("Embedding model evicted — attempting reload (%s)", model)
    return await try_autoload_embedding_model(model, api_base)


async def _try_recover_local_lm_studio_service(
    model: str,
    api_base: str,
    api_key: str | None,
    expected_dim: int | None,
) -> bool:
    """Attempt one bounded LM Studio service recovery after a connection failure."""
    global _last_local_lm_studio_recovery_attempt
    now = time.monotonic()
    if now - _last_local_lm_studio_recovery_attempt < _LOCAL_LM_STUDIO_RECOVERY_COOLDOWN:
        logger.debug("Skipping LM Studio service recovery — cooldown active")
        return False
    _last_local_lm_studio_recovery_attempt = now

    from gobby.cli.services import (
        ensure_local_embedding_service_ready,
        get_local_embedding_service_failure_reason,
    )

    logger.info("Embedding endpoint unavailable — attempting LM Studio recovery (%s)", model)
    recovered = await ensure_local_embedding_service_ready(
        model=model,
        api_base=api_base,
        api_key=api_key,
        expected_dim=expected_dim,
    )
    if recovered:
        return True

    reason = get_local_embedding_service_failure_reason()
    if reason:
        logger.warning("LM Studio recovery failed: %s", reason)
    return False


def _get_api_error_message(error: Exception) -> str:
    """Extract the provider error message from OpenAI-compatible SDK exceptions."""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str):
            return message
        nested = body.get("error")
        if isinstance(nested, dict):
            nested_message = nested.get("message")
            if isinstance(nested_message, str):
                return nested_message
        if isinstance(nested, str):
            return nested
    return str(error)


def _extract_ordered_embeddings(
    response_data: list[Any],
    *,
    requested_count: int,
    model: str,
    api_base: str | None,
) -> list[list[float]]:
    """Order and validate provider response before cache population."""
    try:
        ordered_data = sorted(response_data, key=lambda item: item.index)
        indices = [item.index for item in ordered_data]
        embeddings: list[list[float]] = [item.embedding for item in ordered_data]
    except (AttributeError, TypeError) as e:
        raise EmbeddingGenerationError(
            f"Embedding API returned malformed result for model={model}, api_base={api_base}"
        ) from e

    if not embeddings:
        raise EmbeddingGenerationError(
            f"Embedding API returned empty result for model={model}, api_base={api_base}"
        )
    if len(embeddings) != requested_count:
        raise EmbeddingGenerationError(
            "Embedding API returned an unexpected number of results: "
            f"model={model}, api_base={api_base}, expected={requested_count}, "
            f"actual={len(embeddings)}"
        )
    if indices != list(range(requested_count)):
        raise EmbeddingGenerationError(
            "Embedding API returned invalid result indices: "
            f"model={model}, api_base={api_base}, expected={list(range(requested_count))}, "
            f"actual={indices}"
        )
    for index, embedding in enumerate(embeddings):
        if not embedding:
            raise EmbeddingGenerationError(
                "Embedding API returned an empty vector: "
                f"model={model}, api_base={api_base}, index={index}"
            )
    return embeddings


def _is_ollama_endpoint(api_base: str | None) -> bool:
    """Check if api_base points to an Ollama endpoint (port 11434)."""
    if not api_base:
        return False
    from gobby.cli.services import _is_ollama_endpoint as _services_is_ollama_endpoint

    return _services_is_ollama_endpoint(api_base)


async def _retry_embeddings_after_reload(
    client: Any,
    texts: list[str],
    model: str,
    expected_dim: int | None,
    api_base: str | None,
) -> list[list[float]]:
    """Retry a single embeddings request after the local model is reloaded."""
    raw_response = await client.embeddings.with_raw_response.create(model=model, input=texts)
    embeddings = await asyncio.to_thread(
        _parse_embeddings_response,
        raw_response,
        requested_count=len(texts),
        model=model,
        api_base=api_base,
        expected_dim=expected_dim,
    )
    logger.debug("Generated %s embeddings (%s) after reload", len(embeddings), model)
    return embeddings


def _validate_embeddings_dim(
    embeddings: list[list[float]],
    *,
    expected_dim: int | None,
    model: str,
    api_base: str | None,
) -> None:
    """Fail fast when provider output does not match the configured dimension."""
    if expected_dim is None or not embeddings:
        return

    for index, vector in enumerate(embeddings):
        actual_dim = len(vector)
        if actual_dim == expected_dim:
            continue

        raise EmbeddingGenerationError(
            "Embedding dimension mismatch: "
            f"model={model}, api_base={api_base}, expected_dim={expected_dim}, "
            f"index={index}, actual_dim={actual_dim}"
        )


def _parse_embeddings_response(
    raw_response: Any,
    *,
    requested_count: int,
    model: str,
    api_base: str | None,
    expected_dim: int | None,
) -> list[list[float]]:
    """Deserialize and validate a raw embeddings response.

    CPU-bound: ``raw_response.parse()`` decodes the body and constructs a
    pydantic model per embedding (tens of thousands of floats for a batch),
    and the ordering/dimension checks walk every vector again. Callers must
    run this via ``asyncio.to_thread`` so it stays off the event loop.
    """
    response = raw_response.parse()
    embeddings = _extract_ordered_embeddings(
        response.data,
        requested_count=requested_count,
        model=model,
        api_base=api_base,
    )
    _validate_embeddings_dim(
        embeddings,
        expected_dim=expected_dim,
        model=model,
        api_base=api_base,
    )
    return embeddings


async def _fetch_embeddings(
    texts: list[str],
    model: str,
    api_base: str | None,
    api_key: str | None,
    max_retries: int,
    base_delay: float,
    expected_dim: int | None,
) -> list[list[float]]:
    """Raw API call to generate embeddings (no caching)."""
    provider = _resolve_embedding_provider(
        model=model,
        api_base=api_base,
        api_key=api_key,
    )

    from openai import (
        APIConnectionError,
        APIError,
        AsyncOpenAI,
        AuthenticationError,
        BadRequestError,
        NotFoundError,
        RateLimitError,
    )

    client = AsyncOpenAI(base_url=provider.api_base, api_key=provider.api_key)

    try:
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                raw_response = await client.embeddings.with_raw_response.create(
                    model=provider.model, input=texts
                )
                embeddings = await asyncio.to_thread(
                    _parse_embeddings_response,
                    raw_response,
                    requested_count=len(texts),
                    model=model,
                    api_base=provider.api_base,
                    expected_dim=expected_dim,
                )
                logger.debug("Generated %s embeddings (%s)", len(embeddings), model)
                return embeddings
            except AuthenticationError as e:
                logger.error("Embedding authentication failed: %s", e)
                raise EmbeddingGenerationError(f"Authentication failed: {e}") from e
            except (APIConnectionError, httpx.HTTPError) as e:
                from gobby.cli.services import _is_lm_studio_endpoint

                if not provider.api_base or not _is_lm_studio_endpoint(provider.api_base):
                    logger.error("Failed to generate embeddings: %s", e)
                    raise EmbeddingGenerationError(f"Embedding generation failed: {e}") from e

                recovered = await _try_recover_local_lm_studio_service(
                    model=provider.model,
                    api_base=provider.api_base,
                    api_key=api_key,
                    expected_dim=expected_dim,
                )
                if not recovered:
                    raise EmbeddingGenerationError(f"Embedding generation failed: {e}") from e
                try:
                    return await _retry_embeddings_after_reload(
                        client,
                        texts,
                        provider.model,
                        expected_dim,
                        provider.api_base,
                    )
                except RuntimeError:
                    raise
                except (APIError, httpx.HTTPError) as retry_err:
                    raise EmbeddingGenerationError(
                        f"Embedding failed after LM Studio recovery: {retry_err}"
                    ) from retry_err
            except NotFoundError as e:
                error_message = _get_api_error_message(e).lower()
                if "try pulling it first" not in error_message or not _is_ollama_endpoint(
                    provider.api_base
                ):
                    logger.error("Embedding model not found: %s", e)
                    raise EmbeddingGenerationError(f"Model not found: {e}") from e
                assert (
                    provider.api_base is not None
                )  # guaranteed: _is_ollama_endpoint(provider.api_base) was True above
                reloaded = await _try_reload_model(provider.model, provider.api_base)
                if not reloaded:
                    raise EmbeddingGenerationError(f"Model not found: {e}") from e
                try:
                    return await _retry_embeddings_after_reload(
                        client,
                        texts,
                        provider.model,
                        expected_dim,
                        provider.api_base,
                    )
                except RuntimeError:
                    raise
                except (APIError, httpx.HTTPError) as retry_err:
                    raise EmbeddingGenerationError(
                        f"Embedding failed after model reload: {retry_err}"
                    ) from retry_err
            except BadRequestError as e:
                error_message = _get_api_error_message(e).lower()
                if "no models loaded" not in error_message or not provider.api_base:
                    logger.error("Failed to generate embeddings: %s", e)
                    raise EmbeddingGenerationError(f"Embedding generation failed: {e}") from e
                # Model was evicted from local inference server — try to reload
                reloaded = await _try_reload_model(provider.model, provider.api_base)
                if not reloaded:
                    raise EmbeddingGenerationError(f"Embedding generation failed: {e}") from e
                try:
                    return await _retry_embeddings_after_reload(
                        client,
                        texts,
                        provider.model,
                        expected_dim,
                        provider.api_base,
                    )
                except RuntimeError:
                    raise
                except (APIError, httpx.HTTPError) as retry_err:
                    raise EmbeddingGenerationError(
                        f"Embedding failed after model reload: {retry_err}"
                    ) from retry_err
            except RateLimitError as e:
                last_error = e
                if attempt == max_retries:
                    break
                delay = min(base_delay * (2**attempt), _DEFAULT_MAX_DELAY) * random.uniform(
                    0.8, 1.2
                )  # nosec B311
                logger.warning(
                    "Rate limited (attempt %s/%s), retrying in %.1fs",
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                await asyncio.sleep(delay)
            except RuntimeError:
                raise
            except APIError as e:
                logger.error("Failed to generate embeddings: %s", e)
                raise EmbeddingGenerationError(f"Embedding generation failed: {e}") from e

        logger.error("Rate limit exceeded after %s attempts: %s", max_retries + 1, last_error)
        raise EmbeddingGenerationError(
            f"Rate limit exceeded after {max_retries + 1} attempts: {last_error}"
        ) from last_error
    finally:
        await client.close()


async def _generate_embedding(
    text: str,
    model: str = "nomic-embed-text",
    api_base: str | None = None,
    api_key: str | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    base_delay: float = _DEFAULT_BASE_DELAY,
    is_query: bool = False,
    expected_dim: int | None = None,
    query_prefix: str | None = None,
) -> list[float]:
    """Generate embedding for a single text.

    Convenience wrapper around _generate_embeddings for single texts.

    Args:
        text: Text to embed
        model: Model name
        api_base: Optional API base URL
        api_key: Optional API key
        max_retries: Maximum retry attempts for rate limit errors
        base_delay: Initial backoff delay in seconds
        is_query: Whether this is a query embedding (applies configured/nomic prefix)
        expected_dim: Expected embedding dimension. When set, mismatches fail fast.

    Returns:
        Embedding vector as list of floats

    Raises:
        EmbeddingGenerationError: If embedding provider generation fails
    """
    embeddings = await _generate_embeddings(
        texts=[text],
        model=model,
        api_base=api_base,
        api_key=api_key,
        max_retries=max_retries,
        base_delay=base_delay,
        is_query=is_query,
        expected_dim=expected_dim,
        query_prefix=query_prefix,
    )
    if not embeddings:
        raise EmbeddingGenerationError(
            f"Embedding API returned empty result for model={model}, "
            f"api_base={api_base}, api_key={_masked_key_state(api_key)}"
        )
    return embeddings[0]


def _is_embedding_configured(
    model: str = "nomic-embed-text",
    api_key: str | None = None,
    api_base: str | None = None,
) -> bool:
    """Check whether embedding *configuration* is present.

    This is a pure configuration check; it does **not** probe the endpoint.
    Returns True if a local/custom ``api_base`` is set, or if the model names a
    supported OpenAI cloud embedding model and an explicit ``api_key`` is set.

    A True return only means "we have something to try"; it does not mean
    the endpoint is reachable or the model is loaded. Callers that need a
    real health signal should use ``EmbeddingService.is_reachable`` instead.

    Args:
        model: Model name used to determine whether OpenAI cloud is explicit
        api_key: Optional explicit API key
        api_base: Optional API base URL

    Returns:
        True if embedding config is present, False otherwise.
    """
    if _normalize_api_base(api_base):
        return True

    return is_openai_cloud_embedding_model(model) and bool(api_key)


# ---------------------------------------------------------------------------
# Reachability probe
# ---------------------------------------------------------------------------

# Short TTL so stale failures don't keep callers wedged in fallback mode,
# but long enough that a batch of searches probes once, not N times.
_REACHABILITY_TTL = 30.0  # seconds
_PROBE_TIMEOUT = 3.0  # seconds
_REACHABILITY_CACHE_MAX_SIZE = 64


@dataclass(slots=True)
class _ReachabilityEntry:
    reachable: bool
    checked_at: float


_reachability_cache: dict[tuple[str, str | None], _ReachabilityEntry] = {}


def _clear_reachability_cache() -> None:
    """Clear the reachability probe cache. Exposed for tests."""
    with _get_lock():
        _reachability_cache.clear()


def clear_cache() -> None:
    """Clear generated embedding and reachability caches."""
    _clear_embedding_cache()
    _clear_reachability_cache()


def _reachability_cache_key(api_base: str | None, api_key: str | None) -> tuple[str, str | None]:
    """Key reachability results by endpoint and a non-secret credential fingerprint."""
    api_key_fingerprint = (
        hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12] if api_key else None
    )
    return (api_base or "openai-default", api_key_fingerprint)


def _prune_reachability_cache(now: float, cache_ttl: float, *, incoming: int = 0) -> None:
    """Drop stale entries and bound the cache size while holding the lock."""
    stale_keys = [
        key for key, entry in _reachability_cache.items() if (now - entry.checked_at) >= cache_ttl
    ]
    for key in stale_keys:
        del _reachability_cache[key]

    overflow = len(_reachability_cache) + incoming - _REACHABILITY_CACHE_MAX_SIZE
    if overflow <= 0:
        return

    oldest_first = sorted(_reachability_cache.items(), key=lambda item: item[1].checked_at)
    for key, _entry in oldest_first[:overflow]:
        del _reachability_cache[key]


async def _is_embedding_reachable(
    model: str = "nomic-embed-text",
    api_key: str | None = None,
    api_base: str | None = None,
    timeout: float = _PROBE_TIMEOUT,
    cache_ttl: float = _REACHABILITY_TTL,
) -> bool:
    """Probe the embedding endpoint for actual reachability.

    Short-circuits to False if ``EmbeddingService.is_configured`` is False
    (no config, no probe). Otherwise performs a ``GET {base}/models``
    request with a short timeout. Results are cached per ``(api_base,
    has_key)`` for ``cache_ttl`` seconds to avoid hammering local
    inference servers across a batch of searches.

    The ``/models`` route is part of the OpenAI-compatible surface that
    Ollama, LM Studio, and OpenAI cloud all implement, making it the
    cheapest universal reachability check.

    Args:
        model: Model name (reserved; the probe does not currently filter
            by model since ``/models`` returns the full list)
        api_key: Optional explicit API key
        api_base: Optional API base URL. When omitted, probes OpenAI cloud
            only for an explicit OpenAI embedding model and API key.
        timeout: Per-request timeout in seconds
        cache_ttl: How long to trust a cached probe result

    Returns:
        True if the endpoint answered with a 2xx within the timeout,
        False otherwise (including all exceptions).
    """
    if not _is_embedding_configured(model=model, api_key=api_key, api_base=api_base):
        return False

    normalized_api_base = _normalize_api_base(api_base)
    cache_key = _reachability_cache_key(normalized_api_base, api_key)
    lock = _get_lock()

    now = time.monotonic()
    with lock:
        cached = _reachability_cache.get(cache_key)
        if cached is not None and (now - cached.checked_at) < cache_ttl:
            return cached.reachable

    base = (normalized_api_base or _OPENAI_CLOUD_API_BASE).rstrip("/")
    url = f"{base}/models"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    reachable = False
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
            reachable = 200 <= resp.status_code < 300
            if not reachable:
                logger.debug(
                    "Embedding probe non-2xx: url=%s status=%s",
                    url,
                    resp.status_code,
                )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        logger.debug("Embedding probe failed: url=%s err=%r", url, exc)
        reachable = False

    checked_at = time.monotonic()
    with lock:
        _prune_reachability_cache(checked_at, cache_ttl, incoming=1)
        _reachability_cache[cache_key] = _ReachabilityEntry(
            reachable=reachable,
            checked_at=checked_at,
        )
    return reachable


@dataclass(frozen=True, slots=True)
class EmbeddingService:
    """Daemon embedding service backed by the configured embedding endpoint."""

    model: str = "nomic-embed-text"
    api_base: str | None = None
    api_key: str | None = None
    dim: int | None = None
    query_prefix: str | None = None

    @classmethod
    def from_config(cls, config: EmbeddingsConfig) -> EmbeddingService:
        """Build an embedding service from daemon embedding config."""
        return cls(
            model=config.model,
            api_base=config.api_base,
            api_key=config.api_key,
            dim=config.dim,
            query_prefix=config.query_prefix,
        )

    def is_configured(self, *, model: str | None = None) -> bool:
        """Return whether the service has enough config to attempt embedding."""
        return _is_embedding_configured(
            model=model or self.model,
            api_key=self.api_key,
            api_base=self.api_base,
        )

    async def is_reachable(
        self,
        *,
        model: str | None = None,
        timeout: float = _PROBE_TIMEOUT,
        cache_ttl: float = _REACHABILITY_TTL,
    ) -> bool:
        """Probe endpoint reachability using the configured service settings."""
        return await _is_embedding_reachable(
            model=model or self.model,
            api_key=self.api_key,
            api_base=self.api_base,
            timeout=timeout,
            cache_ttl=cache_ttl,
        )

    async def health_check(self, *, model: str | None = None) -> bool:
        """Generate one small embedding and report whether it succeeded."""
        try:
            result = await self.generate_embedding("health", model=model, max_retries=1)
        except EmbeddingGenerationError as exc:
            logger.warning(
                "Embedding health check failed (model=%s, api_base=%s): %s: %s",
                model or self.model,
                self.api_base,
                type(exc).__name__,
                exc,
            )
            return False
        return bool(result)

    async def generate_embeddings(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        base_delay: float = _DEFAULT_BASE_DELAY,
        is_query: bool = False,
    ) -> list[list[float]]:
        """Generate embeddings with retry, cache, prefixing, and dim validation."""
        return await _generate_embeddings(
            texts=texts,
            model=model or self.model,
            api_base=self.api_base,
            api_key=self.api_key,
            max_retries=max_retries,
            base_delay=base_delay,
            is_query=is_query,
            expected_dim=self.dim,
            query_prefix=self.query_prefix,
        )

    async def generate_embedding(
        self,
        text: str,
        *,
        model: str | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        base_delay: float = _DEFAULT_BASE_DELAY,
        is_query: bool = False,
    ) -> list[float]:
        """Generate one embedding with daemon config."""
        return await _generate_embedding(
            text=text,
            model=model or self.model,
            api_base=self.api_base,
            api_key=self.api_key,
            max_retries=max_retries,
            base_delay=base_delay,
            is_query=is_query,
            expected_dim=self.dim,
            query_prefix=self.query_prefix,
        )

    def clear_cache(self) -> None:
        """Clear generated embedding and reachability caches."""
        clear_cache()
