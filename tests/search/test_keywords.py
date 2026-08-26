"""Tests for YAKE keyword extraction (#20868: warm import, cached extractor, stable output)."""

from __future__ import annotations

import inspect
import subprocess
import sys
import threading

import pytest

from gobby.search import keywords

pytestmark = pytest.mark.unit

# A deliberately conversational prompt: long enough for YAKE to fire and noisy
# enough to clear the extractor's noise threshold.
_NOISY_PROMPT = "hey could you maybe look at the webhook handler thing?"

# Golden output captured on the pre-#20868 implementation (lazy import, fresh
# extractor per call) with yake 0.7.3. The offload must not change extraction.
_NOISY_PROMPT_KEYWORDS = "handler thing webhook hey"


def test_yake_import_is_hoisted_to_module_import() -> None:
    """The per-request lazy import is gone: loading this module loads yake."""
    assert keywords._yake is not None
    assert "yake" in sys.modules


def test_memory_search_import_warms_yake() -> None:
    """Importing the memory search service (part of daemon startup) imports yake,
    so the first post-restart search pays no module loading inside a request.

    Runs in a fresh interpreter so imports from this test session cannot mask a
    regression back to lazy importing.
    """
    code = (
        "import sys\n"
        "assert 'yake' not in sys.modules\n"
        "import gobby.memory.services.search\n"
        "assert 'yake' in sys.modules, 'yake was not imported at module load'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_extractor_is_reused_within_a_thread() -> None:
    """The KeywordExtractor is not rebuilt per call: same config, same instance."""
    first = keywords._get_extractor("en", 10)
    second = keywords._get_extractor("en", 10)
    assert first is second
    assert first.config["dedup_lim"] == 0.9
    assert keywords._get_extractor("en", 5) is not first


def test_dedup_lim_is_a_named_extractor_parameter() -> None:
    """0.9 is also yake's default, so the config assertion alone cannot tell an honoured
    ``dedup_lim`` kwarg from one the extractor swallowed (#20882)."""
    assert keywords._yake is not None
    parameters = inspect.signature(keywords._yake.KeywordExtractor.__init__).parameters
    assert "dedup_lim" in parameters
    assert parameters["dedup_lim"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_extractor_is_not_shared_across_threads() -> None:
    """Extraction mutates instance-level caches, so each thread gets its own."""
    main_thread_extractor = keywords._get_extractor("en", 10)
    seen_in_worker: list[object] = []

    def grab() -> None:
        seen_in_worker.append(keywords._get_extractor("en", 10))

    worker = threading.Thread(target=grab)
    worker.start()
    worker.join(timeout=5.0)

    assert seen_in_worker, "worker thread did not run"
    assert seen_in_worker[0] is not main_thread_extractor


def test_noisy_query_keywords_unchanged_and_stable_across_cached_calls() -> None:
    """Extraction output matches the pre-offload golden, including on the second
    call through the cached extractor."""
    first = keywords.extract_keywords(_NOISY_PROMPT)
    second = keywords.extract_keywords(_NOISY_PROMPT)
    assert first == _NOISY_PROMPT_KEYWORDS
    assert second == _NOISY_PROMPT_KEYWORDS


def test_short_query_skips_extraction() -> None:
    assert keywords.extract_keywords("short query here") is None
