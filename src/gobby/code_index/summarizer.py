"""LLM-based symbol summary generation.

Uses a cheap/fast model (Haiku by default) for one-sentence summaries of code symbols.
Summaries are cached in code_symbols.summary and invalidated on content_hash change
(see CodeIndexStorage.upsert_symbols).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from gobby.code_index.models import Symbol

if TYPE_CHECKING:
    from gobby.config.code_index import CodeIndexConfig
    from gobby.llm.service import LLMService

logger = logging.getLogger(__name__)

_SUMMARY_PROMPT = (
    "Summarize this {kind} in one sentence. Be precise and technical.\n\n"
    "Name: {name}\n"
    "Signature: {signature}\n"
    "Source:\n```\n{source}\n```"
)

_MAX_SOURCE_CHARS = 2000


class SymbolSummarizer:
    """Generates AI summaries for code symbols."""

    def __init__(
        self,
        llm_service: LLMService,
        config: CodeIndexConfig,
    ) -> None:
        self._llm = llm_service
        self._provider_name = config.summary_provider
        self._model_name = config.summary_model

    async def summarize_one(self, symbol: Symbol, source: str) -> str | None:
        """Generate a one-sentence summary for a single symbol.

        Args:
            symbol: The symbol to summarize.
            source: The source code of the symbol.

        Returns:
            Summary string, or None on failure.
        """
        truncated = source[:_MAX_SOURCE_CHARS]
        prompt = _SUMMARY_PROMPT.format(
            kind=symbol.kind,
            name=symbol.name,
            signature=symbol.signature or "",
            source=truncated,
        )

        try:
            provider = self._llm.get_provider(self._provider_name)
        except (ValueError, KeyError):
            logger.debug(f"LLM provider '{self._provider_name}' not available for summaries")
            return None

        try:
            text = await provider.generate_text(
                prompt=prompt,
                model=self._model_name,
                max_tokens=100,
                caller="code_index.symbol_summary",
            )
            text = text.strip()
            return text if text else None
        except Exception as e:
            logger.debug(f"LLM call failed for summary of {symbol.name}: {e}")
            return None

    async def summarize_batch(
        self,
        symbols: list[Symbol],
        read_source: Callable[[Symbol], str | None],
    ) -> dict[str, str]:
        """Generate summaries for a batch of symbols.

        Args:
            symbols: Symbols to summarize.
            read_source: Callable that returns source code for a symbol, or None.

        Returns:
            Dict of {symbol_id: summary} for successful summaries.
        """
        results: dict[str, str] = {}
        for symbol in symbols:
            source = read_source(symbol)
            if not source:
                continue
            summary = await self.summarize_one(symbol, source)
            if summary:
                results[symbol.id] = summary
        return results
