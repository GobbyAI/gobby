"""Keyword extraction for query preprocessing.

Uses YAKE (Yet Another Keyword Extractor) to strip conversational noise
from user prompts before embedding. YAKE is unsupervised, model-free,
and runs in ~1-5ms per query.

Example:
    >>> extract_keywords("hey could you maybe look at the webhook handler thing?")
    'webhook handler'
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Minimum word count to attempt extraction — very short queries
# are already focused enough.
_MIN_WORDS_FOR_EXTRACTION = 6

# If YAKE keywords are less than this fraction of original words,
# the query was noisy and keyword extraction is worthwhile.
_NOISE_THRESHOLD = 0.7


def extract_keywords(
    text: str,
    max_keywords: int = 10,
    language: str = "en",
) -> str | None:
    """Extract keywords from text using YAKE.

    Returns the extracted keyword string if the query is "noisy" (keyword
    text is significantly shorter than raw text), or None if the query is
    already focused and extraction wouldn't help.

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

    try:
        import yake

        extractor = yake.KeywordExtractor(
            lan=language,
            n=2,  # max n-gram size
            top=max_keywords,
            dedupLim=0.3,  # aggressive deduplication
        )
        keywords = extractor.extract_keywords(text)

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
                f"YAKE extraction: {len(words)} words -> {len(result_words)} keywords "
                f"({len(result_words) / len(words):.0%})"
            )
            return result

        return None

    except ImportError as e:
        logger.debug(f"YAKE not installed or failed to import: {e}")
        return None
    except Exception as e:
        logger.debug(f"YAKE extraction failed: {e}")
        return None
