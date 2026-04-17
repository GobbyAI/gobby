"""
Semantic tool search using embeddings.

Provides infrastructure for embedding-based tool discovery:
- Tool embedding storage and retrieval (Qdrant vector store)
- Cosine similarity search
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from gobby.storage.database import DatabaseProtocol

if TYPE_CHECKING:
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

# Default embedding model
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"
DEFAULT_EMBEDDING_DIM = 768


def _cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """
    Compute cosine similarity between two vectors.

    Args:
        vec1: First vector
        vec2: Second vector

    Returns:
        Cosine similarity score between -1 and 1
    """
    if len(vec1) != len(vec2):
        raise ValueError(f"Vector dimension mismatch: {len(vec1)} vs {len(vec2)}")

    dot_product = sum(a * b for a, b in zip(vec1, vec2, strict=True))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return dot_product / (norm1 * norm2)


@dataclass
class SearchResult:
    """Represents a tool search result with similarity score."""

    tool_id: str
    server_name: str
    tool_name: str
    description: str | None
    similarity: float
    embedding_id: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "tool_id": self.tool_id,
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "description": self.description,
            "similarity": round(self.similarity, 4),
        }


def _compute_text_hash(text: str) -> str:
    """Compute SHA-256 hash of text for change detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _build_tool_text(
    name: str, description: str | None, input_schema: dict[str, Any] | None
) -> str:
    """
    Build text representation of a tool for embedding.

    Combines name, description, and parameter info into a single string
    that captures the tool's semantic meaning.
    """
    parts = [f"Tool: {name}"]

    if description:
        parts.append(f"Description: {description}")

    if input_schema:
        # Extract parameter names and descriptions
        properties = input_schema.get("properties", {})
        if properties:
            param_parts = []
            for param_name, param_def in properties.items():
                param_desc = param_def.get("description", "")
                param_type = param_def.get("type", "any")
                if param_desc:
                    param_parts.append(f"{param_name} ({param_type}): {param_desc}")
                else:
                    param_parts.append(f"{param_name} ({param_type})")
            if param_parts:
                parts.append("Parameters: " + ", ".join(param_parts))

    return "\n".join(parts)


class SemanticToolSearch:
    """
    Manages semantic search over MCP tools using embeddings.

    Vectors are stored in Qdrant. Tool metadata (name, description) is
    looked up from the tools/mcp_servers SQLite tables for search results.
    """

    TOOL_COLLECTION = "tool_embeddings"

    def __init__(
        self,
        db: DatabaseProtocol,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        embedding_dim: int = DEFAULT_EMBEDDING_DIM,
        openai_api_key: str | None = None,
        api_base: str | None = None,
        vector_store: VectorStore | None = None,
    ):
        """
        Initialize semantic search manager.

        Args:
            db: Database connection (used for tool metadata lookups in search)
            embedding_model: Model name for embeddings
            embedding_dim: Dimension of embedding vectors
            openai_api_key: API key (not needed for local/ models)
            api_base: API base URL for embedding endpoint
            vector_store: Qdrant vector store for embedding storage/search
        """
        self.db = db
        self.embedding_model = embedding_model
        self.embedding_dim = embedding_dim
        self._openai_api_key = openai_api_key
        self._api_base = api_base
        self._vector_store = vector_store

    @staticmethod
    def _is_dimension_mismatch_error(error: Exception) -> bool:
        """Return True when Qdrant rejected an operation due to vector size mismatch."""
        message = str(error).lower()
        return (
            "vector dimension error" in message
            or "dimension mismatch" in message
            or ("expected dim" in message and "got" in message)
        )

    async def _get_tool_collection_dimension(self) -> int | None:
        """Return the current tool collection dimension when readable."""
        if not self._vector_store:
            return None

        try:
            existing_dim = await self._vector_store.get_collection_dimension(self.TOOL_COLLECTION)
        except Exception:
            return None

        return existing_dim if isinstance(existing_dim, int) else None

    async def _ensure_tool_collection(self, operation: str) -> None:
        """Ensure tool collection exists with the configured embedding dimension."""
        if not self._vector_store:
            return

        existing_dim = await self._get_tool_collection_dimension()
        if existing_dim is not None and existing_dim != self.embedding_dim:
            logger.warning(
                "Semantic tool collection '%s' dimension drift detected before %s "
                "(expected_dim=%s, observed_dim=%s); recreating",
                self.TOOL_COLLECTION,
                operation,
                self.embedding_dim,
                existing_dim,
            )

        await self._vector_store.ensure_collection(self.TOOL_COLLECTION, self.embedding_dim)

        if existing_dim is not None and existing_dim != self.embedding_dim:
            logger.info(
                "Semantic tool collection '%s' repaired before %s "
                "(expected_dim=%s, observed_dim=%s)",
                self.TOOL_COLLECTION,
                operation,
                self.embedding_dim,
                existing_dim,
            )

    async def _repair_tool_collection_and_retry(
        self,
        operation: str,
        error: Exception,
        action: Callable[[], Awaitable[_T]],
    ) -> _T:
        """Repair the tool collection on runtime dimension mismatch and retry once."""
        if not self._vector_store or not self._is_dimension_mismatch_error(error):
            raise error

        existing_dim = await self._get_tool_collection_dimension()
        logger.warning(
            "Semantic tool collection '%s' dimension mismatch during %s "
            "(expected_dim=%s, observed_dim=%s): %s",
            self.TOOL_COLLECTION,
            operation,
            self.embedding_dim,
            existing_dim if existing_dim is not None else "unknown",
            error,
        )
        await self._vector_store.ensure_collection(self.TOOL_COLLECTION, self.embedding_dim)
        result = await action()
        logger.info(
            "Semantic tool collection '%s' repaired and retry succeeded during %s "
            "(expected_dim=%s, observed_dim=%s)",
            self.TOOL_COLLECTION,
            operation,
            self.embedding_dim,
            existing_dim if existing_dim is not None else "unknown",
        )
        return result

    async def store_embedding(
        self,
        tool_id: str,
        server_name: str,
        project_id: str,
        embedding: list[float],
        tool_name: str = "",
        description: str | None = None,
    ) -> None:
        """
        Store a tool embedding in Qdrant.

        Args:
            tool_id: ID of the tool in the tools table
            server_name: Name of the MCP server
            project_id: Project ID
            embedding: Embedding vector as list of floats
            tool_name: Name of the tool (stored in payload for search results)
            description: Tool description (stored in payload for search results)
        """
        if not self._vector_store:
            logger.warning(f"No VectorStore configured - cannot store embedding for tool {tool_id}")
            return

        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()

        payload = {
            "server_name": server_name,
            "tool_name": tool_name,
            "description": description,
            "project_id": project_id,
            "embedding_model": self.embedding_model,
            "updated_at": now,
        }

        async def _upsert() -> None:
            await self._vector_store.upsert(
                memory_id=tool_id,
                embedding=embedding,
                payload=payload,
                collection_name=self.TOOL_COLLECTION,
            )

        await self._ensure_tool_collection("store_embedding")
        try:
            await _upsert()
        except Exception as error:
            await self._repair_tool_collection_and_retry(
                "store_embedding",
                error,
                _upsert,
            )

    async def has_embeddings(self, project_id: str) -> bool:
        """
        Check if any tool embeddings exist for a project in Qdrant.

        Args:
            project_id: Project ID

        Returns:
            True if at least one embedding exists
        """
        if not self._vector_store:
            return False

        try:
            await self._ensure_tool_collection("has_embeddings")

            # Use a dummy query to check for any points with this project_id
            results = await self._vector_store.search(
                query_embedding=[0.0] * self.embedding_dim,
                limit=1,
                filters={"project_id": project_id},
                collection_name=self.TOOL_COLLECTION,
            )
            return len(results) > 0
        except Exception:
            return False

    @staticmethod
    def build_tool_text(
        name: str, description: str | None, input_schema: dict[str, Any] | None
    ) -> str:
        """
        Build text representation of a tool for embedding.

        Public wrapper for the module-level function.

        Args:
            name: Tool name
            description: Tool description
            input_schema: Tool input schema

        Returns:
            Text suitable for embedding
        """
        return _build_tool_text(name, description, input_schema)

    @staticmethod
    def compute_text_hash(text: str) -> str:
        """
        Compute hash of text for change detection.

        Public wrapper for the module-level function.

        Args:
            text: Text to hash

        Returns:
            16-character hex hash
        """
        return _compute_text_hash(text)

    async def embed_text(self, text: str, is_query: bool = False) -> list[float]:
        """
        Generate embedding for text using the shared embedding router.

        Routes to local in-process model (local/ prefix) or cloud API
        (LiteLLM) based on the configured embedding_model.

        Args:
            text: Text to embed
            is_query: If True, use query prefix (for search); False for indexing

        Returns:
            Embedding vector as list of floats

        Raises:
            RuntimeError: If embedding generation fails
        """
        from gobby.search.embeddings import generate_embedding

        return await generate_embedding(
            text=text,
            model=self.embedding_model,
            api_base=self._api_base,
            api_key=self._openai_api_key,
            is_query=is_query,
        )

    async def embed_tool(
        self,
        tool_id: str,
        name: str,
        description: str | None,
        input_schema: dict[str, Any] | None,
        server_name: str,
        project_id: str,
    ) -> bool:
        """
        Generate and store embedding for a tool.

        Always embeds — no hash check. At ~5ms per local embedding,
        re-embedding all tools is fast enough to not need caching.

        Args:
            tool_id: Tool ID
            name: Tool name
            description: Tool description
            input_schema: Tool input schema
            server_name: MCP server name
            project_id: Project ID

        Returns:
            True if embedded successfully
        """
        text = _build_tool_text(name, description, input_schema)
        embedding = await self.embed_text(text)

        await self.store_embedding(
            tool_id=tool_id,
            server_name=server_name,
            project_id=project_id,
            embedding=embedding,
            tool_name=name,
            description=description,
        )
        return True

    async def embed_all_tools(
        self,
        project_id: str,
        mcp_manager: Any,
        internal_manager: Any | None = None,
    ) -> dict[str, Any]:
        """
        Generate embeddings for all tools in a project.

        Iterates through both internal registries and external MCP servers,
        generating embeddings for each tool.

        Args:
            project_id: Project ID
            mcp_manager: LocalMCPManager instance for accessing external tools
            internal_manager: InternalRegistryManager for internal tools (optional)

        Returns:
            Dict with statistics: embedded, failed, by_server
        """
        import uuid

        from gobby.storage.mcp import LocalMCPManager

        if not isinstance(mcp_manager, LocalMCPManager):
            raise TypeError("mcp_manager must be a LocalMCPManager instance")

        stats: dict[str, Any] = {
            "embedded": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
            "by_server": {},
        }

        # Embed internal registry tools (gobby-tasks, gobby-memory, etc.)
        if internal_manager:
            for registry in internal_manager.get_all_registries():
                server_stats = {"embedded": 0, "skipped": 0, "failed": 0}

                for tool_entry in registry.list_tools():
                    tool_name = tool_entry.get("name", "")
                    schema = registry.get_schema(tool_name)
                    description = schema.get("description") if schema else None
                    input_schema = schema.get("inputSchema") if schema else None
                    # Deterministic UUID for internal tools (not in DB)
                    tool_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{registry.name}/{tool_name}"))

                    try:
                        await self.embed_tool(
                            tool_id=tool_id,
                            name=tool_name,
                            description=description,
                            input_schema=input_schema,
                            server_name=registry.name,
                            project_id=project_id,
                        )

                        server_stats["embedded"] += 1
                        stats["embedded"] += 1

                    except Exception as e:
                        server_stats["failed"] += 1
                        stats["failed"] += 1
                        error_msg = f"{registry.name}/{tool_name}: {e}"
                        stats["errors"].append(error_msg)
                        logger.error(f"Failed to embed tool {error_msg}")

                stats["by_server"][registry.name] = server_stats

        # Embed external MCP server tools
        servers = mcp_manager.list_runtime_servers(project_id=project_id, enabled_only=False)

        for server in servers:
            server_stats = {"embedded": 0, "skipped": 0, "failed": 0}

            tools = mcp_manager.get_cached_tools(server.name, project_id=project_id)

            for tool in tools:
                try:
                    await self.embed_tool(
                        tool_id=tool.id,
                        name=tool.name,
                        description=tool.description,
                        input_schema=tool.input_schema,
                        server_name=server.name,
                        project_id=project_id,
                    )

                    server_stats["embedded"] += 1
                    stats["embedded"] += 1

                except Exception as e:
                    server_stats["failed"] += 1
                    stats["failed"] += 1
                    error_msg = f"{server.name}/{tool.name}: {e}"
                    stats["errors"].append(error_msg)
                    logger.error(f"Failed to embed tool {error_msg}")

            stats["by_server"][server.name] = server_stats

        return stats

    async def search_tools(
        self,
        query: str,
        project_id: str,
        top_k: int = 10,
        min_similarity: float = 0.0,
        server_filter: str | None = None,
    ) -> list[SearchResult]:
        """
        Search for tools semantically similar to a query.

        Uses Qdrant vector search.

        Args:
            query: Search query text
            project_id: Project ID to search within
            top_k: Maximum number of results to return
            min_similarity: Minimum similarity threshold (0.0 to 1.0)
            server_filter: Optional server name to filter results

        Returns:
            List of SearchResult sorted by similarity (descending)
        """
        if not self._vector_store:
            logger.warning(
                f"No VectorStore configured - tool search unavailable for query {query!r}"
            )
            return []

        # Embed the query
        query_embedding = await self.embed_text(query, is_query=True)

        filters: dict[str, str] = {"project_id": project_id}
        if server_filter:
            filters["server_name"] = server_filter

        async def _search() -> list[tuple[str, float, dict[str, Any]]]:
            return await self._vector_store.search_with_payload(
                query_embedding=query_embedding,
                limit=top_k,
                filters=filters,
                collection_name=self.TOOL_COLLECTION,
            )

        await self._ensure_tool_collection("search_tools")
        try:
            qdrant_results = await _search()
        except Exception as error:
            qdrant_results = await self._repair_tool_collection_and_retry(
                "search_tools",
                error,
                _search,
            )

        results: list[SearchResult] = []
        for tool_id, score, payload in qdrant_results:
            if score >= min_similarity:
                results.append(
                    SearchResult(
                        tool_id=tool_id,
                        server_name=payload.get("server_name", "unknown"),
                        tool_name=payload.get("tool_name", "unknown"),
                        description=payload.get("description"),
                        similarity=score,
                        embedding_id=0,
                    )
                )
        return results
