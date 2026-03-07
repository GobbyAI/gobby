"""Factory for creating the gobby-code tool registry.

Follows the composite sub-registry pattern from tasks/_factory.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gobby.mcp_proxy.tools.code._context import CodeRegistryContext
from gobby.mcp_proxy.tools.code._graph import create_graph_registry
from gobby.mcp_proxy.tools.code._indexing import create_indexing_registry
from gobby.mcp_proxy.tools.code._query import create_query_registry
from gobby.mcp_proxy.tools.code._summary import create_summary_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry

if TYPE_CHECKING:
    from gobby.code_index.graph import CodeGraph
    from gobby.code_index.indexer import CodeIndexer
    from gobby.code_index.searcher import CodeSearcher
    from gobby.code_index.storage import CodeIndexStorage
    from gobby.code_index.summarizer import SymbolSummarizer
    from gobby.config.code_index import CodeIndexConfig


def create_code_registry(
    storage: CodeIndexStorage,
    indexer: CodeIndexer,
    searcher: CodeSearcher,
    graph: CodeGraph | None = None,
    summarizer: SymbolSummarizer | None = None,
    config: CodeIndexConfig | None = None,
    project_id: str | None = None,
) -> InternalToolRegistry:
    """Create the unified gobby-code tool registry.

    Merges sub-registries for indexing, querying, graph, and summaries.
    """
    ctx = CodeRegistryContext(
        storage=storage,
        indexer=indexer,
        searcher=searcher,
        graph=graph,
        summarizer=summarizer,
        config=config,
        project_id=project_id,
    )

    registry = InternalToolRegistry(
        name="gobby-code",
        description="Code indexing and symbol-level retrieval via tree-sitter AST parsing",
    )

    # Merge all sub-registries
    for sub_factory in (
        create_indexing_registry,
        create_query_registry,
        create_graph_registry,
        create_summary_registry,
    ):
        sub_registry = sub_factory(ctx)
        for tool_name, tool in sub_registry._tools.items():
            registry._tools[tool_name] = tool

    return registry
