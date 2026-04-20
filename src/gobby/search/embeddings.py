"""Embedding generation via OpenAI-compatible API.

Uses the ``openai`` package (already a dependency) which works with any
OpenAI-compatible endpoint: OpenAI cloud, Ollama, LM Studio, etc.

| Provider   | Model                          | Config                                    |
|------------|-------------------------------|-------------------------------------------|
| Ollama     | nomic-embed-text              | api_base=http://localhost:11434/v1        |
| LM Studio  | nomic-embed-text              | api_base=http://localhost:1234/v1         |
| OpenAI     | text-embedding-3-small        | OPENAI_API_KEY                            |

Example usage:
    from gobby.search.embeddings import generate_embeddings, is_embedding_available

    if is_embedding_available("nomic-embed-text", api_base="http://localhost:1234/v1"):
        embeddings = await generate_embeddings(
            texts=["hello world", "foo bar"],
            model="nomic-embed-text",
            api_base="http://localhost:1234/v1",
        )
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Default retry settings for rate-limited requests
_DEFAULT_MAX_RETRIES = 5
_DEFAULT_BASE_DELAY = 1.0  # seconds
_DEFAULT_MAX_DELAY = 60.0  # seconds

# Cooldown for model-reload attempts (prevents hammering lms/ollama)
_RELOAD_COOLDOWN = 60.0  # seconds
_last_reload_attempt: float = 0.0

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
_cache_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Lazy-init the asyncio lock.

    Safe because asyncio is single-threaded: concurrent coroutines in the
    same event loop cannot interleave during this synchronous function body,
    so at most one Lock instance is ever created.
    """
    global _cache_lock  # noqa: PLW0603
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
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


def clear_cache() -> None:
    """Clear the embedding cache. Useful for testing."""
    _cache.clear()


def _needs_nomic_prefix(model: str) -> bool:
    """Check if a model requires nomic-style task prefixes."""
    return "nomic" in model.lower()


def _apply_prefix(text: str, is_query: bool, model: str) -> str:
    """Prepend nomic task prefix when applicable.

    nomic-embed-text was trained with task-specific prefixes:
    - 'search_query: ' for queries
    - 'search_document: ' for documents
    """
    if not _needs_nomic_prefix(model):
        return text
    if is_query:
        return f"search_query: {text}"
    return f"search_document: {text}"


async def generate_embeddings(
    texts: list[str],
    model: str = "nomic-embed-text",
    api_base: str | None = None,
    api_key: str | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    base_delay: float = _DEFAULT_BASE_DELAY,
    is_query: bool = False,
    expected_dim: int | None = None,
) -> list[list[float]]:
    """Generate embeddings using an OpenAI-compatible API with exponential backoff.

    Works with any OpenAI-compatible endpoint (OpenAI cloud, Ollama, LM Studio).
    Rate limit errors are retried with exponential backoff; non-retryable errors
    (auth, model not found) fail immediately.

    Results are cached per (text, model, api_base) with a 60-second TTL to
    deduplicate concurrent identical requests.

    Args:
        texts: List of texts to embed
        model: Model name (e.g., "nomic-embed-text", "text-embedding-3-small")
        api_base: API base URL for OpenAI-compatible endpoint (e.g., "http://localhost:1234/v1" for LM Studio)
        api_key: Optional API key (uses env var OPENAI_API_KEY if not set)
        max_retries: Maximum retry attempts for rate limit errors (default: 5)
        base_delay: Initial backoff delay in seconds (default: 1.0)
        is_query: Whether this is a query embedding (applies nomic prefix when model is nomic)
        expected_dim: Expected embedding dimension. When set, mismatches fail fast.

    Returns:
        List of embedding vectors (one per input text). Returns an empty
        list if the input texts list is empty.

    Raises:
        RuntimeError: If embedding generation fails
    """
    if not texts:
        return []

    # Apply nomic task prefix before cache lookup so prefixed/unprefixed
    # texts cache separately.
    prefixed_texts = [_apply_prefix(t, is_query, model) for t in texts]

    lock = _get_lock()

    # --- Phase 1: Check cache for each text ---
    async with lock:
        _evict_expired()
        results: list[list[float] | None] = []
        miss_indices: list[int] = []
        miss_texts: list[str] = []
        seen_in_batch: dict[str, int] = {}  # key -> first index in results

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
            elif key in seen_in_batch:
                # Duplicate within this batch — will be filled from first occurrence
                results.append(None)
                miss_indices.append(i)
            else:
                results.append(None)
                miss_indices.append(i)
                miss_texts.append(text)
                seen_in_batch[key] = i

    # --- Phase 2: Fetch uncached embeddings ---
    if miss_texts:
        fresh = await _fetch_embeddings(
            texts=miss_texts,
            model=model,
            api_base=api_base,
            api_key=api_key,
            max_retries=max_retries,
            base_delay=base_delay,
            expected_dim=expected_dim,
        )

        # --- Phase 3: Store results in cache ---
        async with lock:
            now = time.monotonic()
            expires_at = now + _CACHE_TTL

            # Map miss_texts back to their embeddings
            text_to_embedding: dict[str, list[float]] = {}
            for text, emb in zip(miss_texts, fresh, strict=True):
                key = _cache_key(text, model, api_base)
                _cache[key] = _CacheEntry(embedding=emb, expires_at=expires_at)
                text_to_embedding[text] = emb

            _enforce_max_size()

        # Fill in the None slots
        for i in miss_indices:
            text = prefixed_texts[i]
            results[i] = text_to_embedding.get(text)

    # All slots should be filled now
    return results  # type: ignore[return-value]


async def _try_reload_model(model: str, api_base: str) -> bool:
    """Attempt to reload an evicted model on a local inference server.

    Respects a cooldown to avoid hammering the server when multiple
    concurrent calls all see the same eviction error.
    """
    global _last_reload_attempt  # noqa: PLW0603
    now = time.monotonic()
    if now - _last_reload_attempt < _RELOAD_COOLDOWN:
        logger.debug("Skipping model reload — cooldown active")
        return False
    _last_reload_attempt = now

    from gobby.cli.services import try_autoload_embedding_model

    logger.info(f"Embedding model evicted — attempting reload ({model})")
    return await try_autoload_embedding_model(model, api_base)


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


def _is_ollama_endpoint(api_base: str | None) -> bool:
    """Check if api_base points to an Ollama endpoint (port 11434)."""
    if not api_base:
        return False
    try:
        return urlparse(api_base).port == 11434
    except (ValueError, AttributeError):
        return False


async def _retry_embeddings_after_reload(
    client: Any,
    texts: list[str],
    model: str,
    expected_dim: int | None,
    api_base: str | None,
) -> list[list[float]]:
    """Retry a single embeddings request after the local model is reloaded."""
    response = await client.embeddings.create(model=model, input=texts)
    embeddings = [item.embedding for item in response.data]
    _validate_embeddings_dim(
        embeddings,
        expected_dim=expected_dim,
        model=model,
        api_base=api_base,
    )
    logger.debug(f"Generated {len(embeddings)} embeddings ({model}) after reload")
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

        raise RuntimeError(
            "Embedding dimension mismatch: "
            f"model={model}, api_base={api_base}, expected_dim={expected_dim}, "
            f"index={index}, actual_dim={actual_dim}"
        )


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
    from openai import (
        AsyncOpenAI,
        AuthenticationError,
        BadRequestError,
        NotFoundError,
        RateLimitError,
    )

    # Use "unused" as default key for local endpoints (Ollama doesn't need a key)
    effective_key = api_key or os.environ.get("OPENAI_API_KEY") or "unused"
    client = AsyncOpenAI(base_url=api_base, api_key=effective_key)

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = await client.embeddings.create(model=model, input=texts)
            embeddings: list[list[float]] = [item.embedding for item in response.data]
            _validate_embeddings_dim(
                embeddings,
                expected_dim=expected_dim,
                model=model,
                api_base=api_base,
            )
            logger.debug(f"Generated {len(embeddings)} embeddings ({model})")
            return embeddings
        except AuthenticationError as e:
            logger.error(f"Embedding authentication failed: {e}")
            raise RuntimeError(f"Authentication failed: {e}") from e
        except NotFoundError as e:
            error_message = _get_api_error_message(e).lower()
            if "try pulling it first" not in error_message or not _is_ollama_endpoint(api_base):
                logger.error(f"Embedding model not found: {e}")
                raise RuntimeError(f"Model not found: {e}") from e
            assert api_base is not None  # guaranteed: _is_ollama_endpoint(api_base) was True above
            reloaded = await _try_reload_model(model, api_base)
            if not reloaded:
                raise RuntimeError(f"Model not found: {e}") from e
            try:
                return await _retry_embeddings_after_reload(
                    client,
                    texts,
                    model,
                    expected_dim,
                    api_base,
                )
            except RuntimeError:
                raise
            except Exception as retry_err:
                raise RuntimeError(
                    f"Embedding failed after model reload: {retry_err}"
                ) from retry_err
        except BadRequestError as e:
            error_message = _get_api_error_message(e).lower()
            if "no models loaded" not in error_message or not api_base:
                logger.error(f"Failed to generate embeddings: {e}")
                raise RuntimeError(f"Embedding generation failed: {e}") from e
            # Model was evicted from local inference server — try to reload
            reloaded = await _try_reload_model(model, api_base)
            if not reloaded:
                raise RuntimeError(f"Embedding generation failed: {e}") from e
            try:
                return await _retry_embeddings_after_reload(
                    client,
                    texts,
                    model,
                    expected_dim,
                    api_base,
                )
            except RuntimeError:
                raise
            except Exception as retry_err:
                raise RuntimeError(
                    f"Embedding failed after model reload: {retry_err}"
                ) from retry_err
        except RateLimitError as e:
            last_error = e
            if attempt == max_retries:
                break
            delay = min(base_delay * (2**attempt), _DEFAULT_MAX_DELAY) * random.uniform(0.8, 1.2)  # nosec B311
            logger.warning(
                f"Rate limited (attempt {attempt + 1}/{max_retries + 1}), retrying in {delay:.1f}s"
            )
            await asyncio.sleep(delay)
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"Failed to generate embeddings: {e}")
            raise RuntimeError(f"Embedding generation failed: {e}") from e

    logger.error(f"Rate limit exceeded after {max_retries + 1} attempts: {last_error}")
    raise RuntimeError(
        f"Rate limit exceeded after {max_retries + 1} attempts: {last_error}"
    ) from last_error


async def generate_embedding(
    text: str,
    model: str = "nomic-embed-text",
    api_base: str | None = None,
    api_key: str | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    base_delay: float = _DEFAULT_BASE_DELAY,
    is_query: bool = False,
    expected_dim: int | None = None,
) -> list[float]:
    """Generate embedding for a single text.

    Convenience wrapper around generate_embeddings for single texts.

    Args:
        text: Text to embed
        model: Model name
        api_base: Optional API base URL
        api_key: Optional API key
        max_retries: Maximum retry attempts for rate limit errors
        base_delay: Initial backoff delay in seconds
        is_query: Whether this is a query embedding (applies nomic prefix when model is nomic)
        expected_dim: Expected embedding dimension. When set, mismatches fail fast.

    Returns:
        Embedding vector as list of floats

    Raises:
        RuntimeError: If embedding generation fails
    """
    embeddings = await generate_embeddings(
        texts=[text],
        model=model,
        api_base=api_base,
        api_key=api_key,
        max_retries=max_retries,
        base_delay=base_delay,
        is_query=is_query,
        expected_dim=expected_dim,
    )
    if not embeddings:
        raise RuntimeError(
            f"Embedding API returned empty result for model={model}, "
            f"api_base={api_base}, api_key={'[set]' if api_key else '[not set]'}"
        )
    return embeddings[0]


def is_embedding_available(
    model: str = "nomic-embed-text",
    api_key: str | None = None,
    api_base: str | None = None,
) -> bool:
    """Check if embedding is available for the given model.

    If api_base is set (LM Studio, Ollama, custom endpoints), assumes available.
    Otherwise, requires an API key.

    Args:
        model: Model name
        api_key: Optional explicit API key
        api_base: Optional API base URL

    Returns:
        True if embeddings can be generated, False otherwise
    """
    # Local endpoints (Ollama, LM Studio) are assumed available
    if api_base:
        return True

    # Cloud models need an API key
    effective_key = api_key or os.environ.get("OPENAI_API_KEY")
    return effective_key is not None and len(effective_key) > 0
