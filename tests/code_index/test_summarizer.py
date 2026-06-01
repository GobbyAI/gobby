"""Tests for code_index.summarizer."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.ai import (
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    CapabilityUnavailableError,
    TextGenerationRequest,
    TextGenerationService,
)
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


class _FakeTextGenerateAdapter:
    def __init__(
        self,
        *,
        response: str = "Returns a greeting string.",
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.requests: list[TextGenerationRequest] = []

    async def generate(self, request: TextGenerationRequest) -> str:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def _text_generation_service(
    adapter: _FakeTextGenerateAdapter | None = None,
    *,
    available: bool = True,
) -> TextGenerationService:
    binding = (
        CapabilityBinding(
            capability=AICapability.TEXT_GENERATE,
            provider="claude",
            adapter_style=AIAdapterStyle.LLM_PROVIDER,
            available=True,
            models=("haiku",),
        )
        if available
        else CapabilityBinding.unavailable(
            AICapability.TEXT_GENERATE,
            provider="claude",
            adapter_style=AIAdapterStyle.LLM_PROVIDER,
            reason="claude text_generate is unavailable",
            models=("haiku",),
        )
    )
    adapters = {"claude": adapter} if adapter is not None else {}
    return TextGenerationService(AICapabilityRegistry([binding]), adapters)


@pytest.fixture
def fake_text_adapter() -> _FakeTextGenerateAdapter:
    return _FakeTextGenerateAdapter()


@pytest.fixture
def summarizer(fake_text_adapter: _FakeTextGenerateAdapter) -> SymbolSummarizer:
    return SymbolSummarizer(_text_generation_service(fake_text_adapter), _make_config())


@pytest.mark.asyncio
async def test_summarize_one(
    summarizer: SymbolSummarizer,
    fake_text_adapter: _FakeTextGenerateAdapter,
) -> None:
    """summarize_one returns the text_generate summary."""
    sym = _make_symbol()
    result = await summarizer.summarize_one(sym, "def greet(): return 'hello'")
    assert result == "Returns a greeting string."

    request = fake_text_adapter.requests[0]
    assert request.caller == "code_index.symbol_summary"
    assert request.provider == "claude"
    assert request.model == "haiku"
    assert request.max_tokens == 100


@pytest.mark.asyncio
async def test_summarize_one_truncates_source(
    fake_text_adapter: _FakeTextGenerateAdapter,
    summarizer: SymbolSummarizer,
) -> None:
    """Source longer than _MAX_SOURCE_CHARS is truncated."""
    sym = _make_symbol()
    long_source = "x" * (_MAX_SOURCE_CHARS + 500)
    await summarizer.summarize_one(sym, long_source)

    prompt = fake_text_adapter.requests[0].prompt
    assert len(prompt) < len(long_source)


@pytest.mark.asyncio
async def test_summarize_one_generation_error() -> None:
    """text_generate errors return None gracefully."""
    service = _text_generation_service(_FakeTextGenerateAdapter(error=RuntimeError("API error")))
    summarizer = SymbolSummarizer(service, _make_config())

    result = await summarizer.summarize_one(_make_symbol(), "source code")
    assert result is None


@pytest.mark.asyncio
async def test_summarize_one_provider_not_available() -> None:
    """Missing provider returns None."""
    service = _text_generation_service(available=False)
    summarizer = SymbolSummarizer(service, _make_config())

    result = await summarizer.summarize_one(_make_symbol(), "source")
    assert result is None


@pytest.mark.asyncio
async def test_summarize_one_missing_adapter_returns_none() -> None:
    """A selected binding without an adapter returns None."""
    service = _text_generation_service(adapter=None)
    summarizer = SymbolSummarizer(service, _make_config())

    result = await summarizer.summarize_one(_make_symbol(), "source")
    assert result is None


@pytest.mark.asyncio
async def test_summarize_one_capability_error_returns_none() -> None:
    """CapabilityUnavailableError is handled like other generation failures."""
    service = MagicMock()
    service.generate.side_effect = CapabilityUnavailableError(
        AICapability.TEXT_GENERATE,
        provider="claude",
        reason="No binding",
    )
    summarizer = SymbolSummarizer(service, _make_config())

    result = await summarizer.summarize_one(_make_symbol(), "source")
    assert result is None


@pytest.mark.asyncio
async def test_summarize_one_empty_response() -> None:
    """Empty text_generate response returns None."""
    service = _text_generation_service(_FakeTextGenerateAdapter(response="   "))
    summarizer = SymbolSummarizer(service, _make_config())

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
