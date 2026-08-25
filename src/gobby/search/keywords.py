"""Keyword extraction for query preprocessing.

Uses YAKE (Yet Another Keyword Extractor) to strip conversational noise
from user prompts before embedding. YAKE is unsupervised, model-free,
and runs in ~1-5ms per query.

``extract_keywords`` is CPU-bound synchronous work: async callers on the
daemon event loop must run it via ``asyncio.to_thread`` (#20868).

Example with the configured ``dedup_lim=0.9`` threshold:
    >>> extract_keywords("hey could you maybe look at the webhook handler thing?")
    'handler thing webhook hey'
"""

from __future__ import annotations

import logging
import threading
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)

# Imported at module load so no request ever pays module loading: the daemon's
# memory search service imports this module at startup, which warms YAKE before
# the first post-restart search (#20868). Guarded so a broken YAKE install
# degrades to raw-query search instead of taking the memory service down.
_yake: ModuleType | None
try:
    import yake as _yake_module
except ImportError as import_error:
    logger.debug("YAKE not installed or failed to import: %s", import_error)
    _yake = None
else:
    _yake = _yake_module

# Minimum word count to attempt extraction — very short queries
# are already focused enough.
_MIN_WORDS_FOR_EXTRACTION = 6

# If YAKE keywords are less than this fraction of original words,
# the query was noisy and keyword extraction is worthwhile.
_NOISE_THRESHOLD = 0.7

# One extractor per thread per (language, max_keywords): construction reads
# stopword files from disk, so instances are reused across calls — but never
# across threads, because extraction mutates instance-level similarity caches
# and stats (#20868 runs extraction in asyncio.to_thread executor workers).
_extractor_cache = threading.local()


def _get_extractor(language: str, max_keywords: int) -> Any:
    """Return this thread's cached ``KeywordExtractor`` for the given config."""
    if _yake is None:
        # extract_keywords() checks availability first; defensive for other callers.
        raise RuntimeError("YAKE is not available")
    cache: dict[tuple[str, int], Any] | None = getattr(_extractor_cache, "extractors", None)
    if cache is None:
        cache = {}
        _extractor_cache.extractors = cache
    key = (language, max_keywords)
    extractor = cache.get(key)
    if extractor is None:
        # Keep YAKE's 0.9 deduplication threshold explicit so dependency defaults
        # cannot change the extraction behavior established by the golden test.
        extractor = _yake.KeywordExtractor(
            lan=language,
            n=2,  # max n-gram size
            top=max_keywords,
            dedup_lim=0.9,
        )
        cache[key] = extractor
    return extractor


def extract_keywords(
    text: str,
    max_keywords: int = 10,
    language: str = "en",
) -> str | None:
    """Extract keywords from text using YAKE.

    Returns the extracted keyword string if the query is "noisy" (keyword
    text is significantly shorter than raw text), or None if the query is
    already focused and extraction wouldn't help.

    CPU-bound and synchronous by design; event-loop callers must offload it
    with ``asyncio.to_thread`` (#20868).

    Args:
        text: Input text to extract keywords from
        max_keywords: Maximum number of keywords to extract
        language: Language code for YAKE

    Returns:
        Space-joined keyword string, or None if extraction not worthwhile
    """
    words = text.split()
    if len(words) < _MIN_WORDS_FOR_EXTRACTION:
        return None
    if _yake is None:
        return None

    try:
        keywords = _get_extractor(language, max_keywords).extract_keywords(text)

        if not keywords:
            return None

        # Keywords are (keyword_text, score) tuples, lower score = more relevant.
        # YAKE returns overlapping n-grams ("webhook handler", "handler thing").
        # Collect unique words in relevance order to avoid duplication.
        seen: set[str] = set()
        unique_words: list[str] = []
        for kw, _score in keywords:
            for word in kw.split():
                lower = word.lower()
                if lower not in seen:
                    seen.add(lower)
                    unique_words.append(word)

        result = " ".join(unique_words)

        # Only use keywords if they're significantly shorter than original
        result_words = result.split()
        if len(result_words) / len(words) < _NOISE_THRESHOLD:
            logger.debug(
                "YAKE extraction: %s words -> %s keywords (%s)",
                len(words),
                len(result_words),
                format(len(result_words) / len(words), ".0%"),
            )
            return result

        return None

    except Exception as e:
        logger.debug("YAKE extraction failed: %s", e)
        return None
