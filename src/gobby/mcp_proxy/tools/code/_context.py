"""Shared context for code tool sub-registries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.code_index.graph import CodeGraph
    from gobby.code_index.indexer import CodeIndexer
    from gobby.code_index.searcher import CodeSearcher
    from gobby.code_index.storage import CodeIndexStorage
    from gobby.code_index.summarizer import SymbolSummarizer
    from gobby.config.code_index import CodeIndexConfig


@dataclass
class CodeRegistryContext:
    """Dependencies shared across code tool sub-registries."""

    storage: CodeIndexStorage
    indexer: CodeIndexer
    searcher: CodeSearcher
    graph: CodeGraph | None = None
    summarizer: SymbolSummarizer | None = None
    config: CodeIndexConfig | None = None
    project_id: str | None = None
