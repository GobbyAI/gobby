"""LLM-based symbol summary generation.

Uses Haiku/flash for cheap, fast one-sentence summaries of code symbols.
Summaries are cached in code_symbols.summary and invalidated on content_hash change.
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
        self._batch_size = config.summary_batch_size

    async def generate_summaries(
        self,
        symbols: list[Symbol],
        source_reader: Callable[[str, int, int], str | None],
    ) -> dict[str, str]:
        """Generate summaries for a batch of symbols.

        Args:
            symbols: Symbols to summarize.
            source_reader: Callable(file_path, byte_start, byte_end) -> source text.

        Returns:
            Map of symbol_id -> summary text.
        """
        results: dict[str, str] = {}

        for sym in symbols[: self._batch_size]:
            source = source_reader(sym.file_path, sym.byte_start, sym.byte_end)
            if source is None:
                continue

            try:
                summary = await self.generate_summary(sym, source)
                if summary:
                    results[sym.id] = summary
            except Exception as e:
                logger.debug(f"Summary generation failed for {sym.qualified_name}: {e}")

        return results

    async def generate_summary(self, symbol: Symbol, source: str) -> str | None:
        """Generate a summary for a single symbol."""
        # Truncate source to prevent excessive token usage
        max_source = 500
        truncated = source[:max_source] + ("..." if len(source) > max_source else "")

        prompt = _SUMMARY_PROMPT.format(
            kind=symbol.kind,
            name=symbol.qualified_name,
            signature=symbol.signature or symbol.name,
            source=truncated,
        )

        try:
            provider = self._llm.get_provider(self._provider_name)
            if provider is None:
                return None

            text = await provider.generate_text(
                prompt=prompt,
                model=self._model_name,
                max_tokens=100,
            )

            text = text.strip()
            return text if text else None

        except Exception as e:
            logger.debug(f"LLM call failed for summary: {e}")
            return None
