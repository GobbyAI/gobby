"""Process-wide embedding memoization and ownership of concurrent cache fills."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass
from threading import RLock


class EmbeddingGenerationError(RuntimeError):
    """Raised when embedding generation or a cache fill fails."""


type EmbeddingFetcher = Callable[[list[str]], Awaitable[list[list[float]]]]

_CACHE_TTL = 60.0
_CACHE_MAX_SIZE = 2048


@dataclass(slots=True)
class _CacheEntry:
    embedding: list[float]
    expires_at: float


_cache: dict[str, _CacheEntry] = {}
_inflight: dict[str, Future[list[float]]] = {}
# Import initialization is serialized; all loops and threads share this mutex.
_cache_lock = RLock()


def _cache_key(text: str, model: str, api_base: str | None) -> str:
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    return f"{digest}:{model}:{api_base or 'default'}"


def _evict_expired() -> None:
    now = time.monotonic()
    expired = [key for key, entry in _cache.items() if entry.expires_at <= now]
    for key in expired:
        del _cache[key]


def _enforce_max_size() -> None:
    if len(_cache) <= _CACHE_MAX_SIZE:
        return
    by_expiry = sorted(_cache.items(), key=lambda item: item[1].expires_at)
    for key, _entry in by_expiry[: len(_cache) - _CACHE_MAX_SIZE]:
        del _cache[key]


def clear_cache() -> None:
    """Invalidate memoized results and release waiters to retry abandoned fills."""
    with _cache_lock:
        _cache.clear()
        for future in _inflight.values():
            future.cancel()
        _inflight.clear()


def _release_fills(
    fills: list[tuple[str, Future[list[float]]]], error: Exception | None = None
) -> None:
    with _cache_lock:
        for key, future in fills:
            if _inflight.get(key) is future:
                del _inflight[key]
            if not future.done():
                if error is None:
                    future.cancel()
                else:
                    future.set_exception(error)


async def generate_cached_embeddings(
    texts: list[str],
    *,
    model: str,
    api_base: str | None,
    expected_dim: int | None,
    fetch: EmbeddingFetcher,
) -> list[list[float]]:
    """Deduplicate prepared texts without sharing caller cancellation or loops."""
    if not texts:
        return []

    while True:
        with _cache_lock:
            _evict_expired()
            results: list[list[float] | None] = []
            pending: list[tuple[int, str, Future[list[float]]]] = []
            new_texts: list[str] = []
            new_fills: list[tuple[str, Future[list[float]]]] = []

            for index, text in enumerate(texts):
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
                        # A concurrent Future has no event-loop affinity. Each
                        # waiter wraps it on its own loop below.
                        future = Future()
                        _inflight[key] = future
                        new_texts.append(text)
                        new_fills.append((key, future))
                    results.append(None)
                    pending.append((index, key, future))

        if new_texts:
            try:
                fresh = await fetch(new_texts)
                if len(fresh) != len(new_fills):
                    raise EmbeddingGenerationError("Embedding cache fill returned the wrong count")
                # A wrong-dimension fill fails every waiter and caches nothing.
                for embedding in fresh:
                    if expected_dim is not None and len(embedding) != expected_dim:
                        raise EmbeddingGenerationError(
                            f"Embedding dimension mismatch for model={model}: "
                            f"expected {expected_dim}, got {len(embedding)}"
                        )
            except asyncio.CancelledError:
                # Only the producer owns these fills. Release them before
                # propagating its real cancellation; surviving callers retry.
                _release_fills(new_fills)
                raise
            except Exception as exc:
                _release_fills(new_fills, exc)
                raise

            with _cache_lock:
                expires_at = time.monotonic() + _CACHE_TTL
                for (key, future), embedding in zip(new_fills, fresh, strict=True):
                    # A clear may have invalidated this producer while it fetched.
                    # It must not publish over a newer fill for the same key.
                    if _inflight.get(key) is future:
                        _cache[key] = _CacheEntry(embedding=embedding, expires_at=expires_at)
                        del _inflight[key]
                    if not future.done():
                        future.set_result(embedding)
                _enforce_max_size()

        abandoned = False
        for index, key, future in pending:
            try:
                embedding = await asyncio.shield(asyncio.wrap_future(future))
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                if not future.cancelled():
                    raise
                # The owning producer or a cache clear abandoned this fill.
                # Reuse completed cache entries and claim only remaining misses.
                abandoned = True
                break
            if expected_dim is not None and len(embedding) != expected_dim:
                with _cache_lock:
                    _cache.pop(key, None)
                raise EmbeddingGenerationError(
                    f"Embedding dimension mismatch for model={model}: "
                    f"expected {expected_dim}, got {len(embedding)}"
                )
            results[index] = embedding

        if abandoned:
            continue
        filled_results: list[list[float]] = []
        for result in results:
            if result is None:
                raise EmbeddingGenerationError("Embedding cache fill left a missing result")
            filled_results.append(result)
        return filled_results
