"""Tests for code_index.summarizer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.code_index.models import Symbol
from gobby.code_index.summarizer import _MAX_SOURCE_CHARS, SymbolSummarizer

pytestmark = pytest.mark.unit


def _make_config(**overrides: object) -> MagicMock:
    cfg = MagicMock()
    cfg.summary_provider = overrides.get("summary_provider", "claude")
    cfg.summary_model = overrides.get("summary_model", "haiku")
    return cfg


def _make_symbol(name: str = "greet", kind: str = "function") -> Symbol:
    return Symbol(
        id="sym-1",
        project_id="proj-1",
        file_path="src/app.py",
        name=name,
        qualified_name=name,
        kind=kind,
        language="python",
        byte_start=0,
        byte_end=100,
        line_start=1,
        line_end=5,
        signature=f"def {name}() -> str:",
        content_hash="abc123",
    )


@pytest.fixture
def mock_llm_service() -> MagicMock:
    service = MagicMock()
    provider = MagicMock()
    provider.generate_text = AsyncMock(return_value="Returns a greeting string.")
    service.get_provider.return_value = provider
    return service


@pytest.fixture
def summarizer(mock_llm_service: MagicMock) -> SymbolSummarizer:
    return SymbolSummarizer(mock_llm_service, _make_config())


@pytest.mark.asyncio
async def test_summarize_one(summarizer: SymbolSummarizer) -> None:
    """summarize_one returns the LLM-generated summary."""
    sym = _make_symbol()
    result = await summarizer.summarize_one(sym, "def greet(): return 'hello'")
    assert result == "Returns a greeting string."

    provider = summarizer._llm.get_provider.return_value
    assert provider.generate_text.await_args.kwargs["caller"] == "code_index.symbol_summary"


@pytest.mark.asyncio
async def test_summarize_one_truncates_source(
    mock_llm_service: MagicMock,
    summarizer: SymbolSummarizer,
) -> None:
    """Source longer than _MAX_SOURCE_CHARS is truncated."""
    sym = _make_symbol()
    long_source = "x" * (_MAX_SOURCE_CHARS + 500)
    await summarizer.summarize_one(sym, long_source)

    # Check the prompt passed to generate_text
    provider = mock_llm_service.get_provider.return_value
    call_args = provider.generate_text.call_args
    prompt = call_args.kwargs.get("prompt") or call_args[1].get("prompt") or call_args[0][0]
    assert len(prompt) < len(long_source)


@pytest.mark.asyncio
async def test_summarize_one_llm_error(mock_llm_service: MagicMock) -> None:
    """LLM errors return None gracefully."""
    provider = mock_llm_service.get_provider.return_value
    provider.generate_text = AsyncMock(side_effect=RuntimeError("API error"))
    summarizer = SymbolSummarizer(mock_llm_service, _make_config())

    result = await summarizer.summarize_one(_make_symbol(), "source code")
    assert result is None


@pytest.mark.asyncio
async def test_summarize_one_provider_not_available() -> None:
    """Missing provider returns None."""
    service = MagicMock()
    service.get_provider.side_effect = ValueError("not configured")
    summarizer = SymbolSummarizer(service, _make_config())

    result = await summarizer.summarize_one(_make_symbol(), "source")
    assert result is None


@pytest.mark.asyncio
async def test_summarize_one_empty_response(mock_llm_service: MagicMock) -> None:
    """Empty LLM response returns None."""
    provider = mock_llm_service.get_provider.return_value
    provider.generate_text = AsyncMock(return_value="   ")
    summarizer = SymbolSummarizer(mock_llm_service, _make_config())

    result = await summarizer.summarize_one(_make_symbol(), "source")
    assert result is None


@pytest.mark.asyncio
async def test_summarize_batch(summarizer: SymbolSummarizer) -> None:
    """summarize_batch processes symbols and returns {id: summary}."""
    sym1 = _make_symbol("greet")
    sym1.id = "sym-1"
    sym2 = _make_symbol("farewell")
    sym2.id = "sym-2"

    def read_source(sym: Symbol) -> str | None:
        return f"def {sym.name}(): pass"

    results = await summarizer.summarize_batch([sym1, sym2], read_source)
    assert len(results) == 2
    assert "sym-1" in results
    assert "sym-2" in results


@pytest.mark.asyncio
async def test_summarize_batch_skips_missing_source(
    summarizer: SymbolSummarizer,
) -> None:
    """Symbols with no readable source are skipped."""
    sym = _make_symbol()

    def read_source(sym: Symbol) -> str | None:
        return None

    results = await summarizer.summarize_batch([sym], read_source)
    assert len(results) == 0
