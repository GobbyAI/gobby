"""Qdrant indexing for GitHub issue triage."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from qdrant_client.http.exceptions import ResponseHandlingException

from gobby.memory.vectorstore import VectorStoreUnavailableError

GITHUB_ISSUE_COLLECTION = "gobby_github_issues"
_POINT_NAMESPACE = uuid.UUID("75c10517-1a5d-4d31-a102-0e8694f09cc0")
logger = logging.getLogger(__name__)

Embedding = Sequence[float]
SearchResult = tuple[str, float, dict[str, Any]]


class VectorStoreProtocol(Protocol):
    """Vector store methods used by GitHub issue indexing."""

    async def ensure_collection(self, collection_name: str) -> None:
        """Ensure the target vector collection exists."""
        ...

    async def upsert(
        self,
        point_id: str,
        embedding: Embedding,
        payload: dict[str, Any],
        *,
        collection_name: str,
    ) -> None:
        """Upsert an embedding payload."""
        ...

    async def search_with_payload(
        self,
        embedding: Embedding,
        *,
        limit: int,
        filters: dict[str, Any],
        collection_name: str,
    ) -> list[SearchResult]:
        """Search embeddings and return point ids, scores, and payloads."""
        ...


class EmbedFnProtocol(Protocol):
    """Async embedding function used by GitHub issue indexing."""

    async def __call__(self, text: str) -> Embedding:
        """Embed text into a vector."""
        ...


@dataclass(frozen=True)
class IssueSnapshot:
    """Normalized GitHub issue data used for triage and indexing."""

    project_id: str
    repo: str
    issue_number: int
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    updated_at: str | None
    issue_url: str | None

    @classmethod
    def from_github(
        cls,
        *,
        project_id: str,
        repo: str,
        issue: dict[str, Any],
    ) -> IssueSnapshot:
        labels = []
        for label in issue.get("labels") or []:
            if isinstance(label, dict):
                name = label.get("name")
            else:
                name = label
            if name:
                labels.append(str(name))

        return cls(
            project_id=project_id,
            repo=repo,
            issue_number=int(issue["number"]),
            title=str(issue.get("title") or "Untitled issue"),
            body=str(issue.get("body") or ""),
            state=str(issue.get("state") or "open"),
            labels=tuple(sorted(labels)),
            updated_at=issue.get("updated_at") or issue.get("updatedAt"),
            issue_url=issue.get("html_url") or issue.get("url"),
        )

    @property
    def issue_key(self) -> str:
        return f"{self.repo}#{self.issue_number}"


@dataclass(frozen=True)
class IssueDuplicate:
    """Semantic duplicate candidate returned from Qdrant."""

    repo: str
    issue_number: int
    issue_url: str | None
    score: float
    task_id: str | None = None

    @property
    def issue_key(self) -> str:
        return f"{self.repo}#{self.issue_number}"


def issue_point_id(project_id: str, repo: str, issue_number: int) -> str:
    """Return deterministic Qdrant point id for a project/repo/issue tuple."""
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{project_id}:{repo}:{issue_number}"))


def build_issue_content(issue: IssueSnapshot) -> str:
    """Build stable text used for semantic issue deduplication."""
    labels = ", ".join(issue.labels)
    return "\n".join(
        [
            f"Title: {issue.title}",
            f"Repository: {issue.repo}",
            f"Labels: {labels}",
            "",
            issue.body,
        ]
    ).strip()


def content_hash(issue: IssueSnapshot) -> str:
    """Hash the parts of an issue that should trigger re-triage."""
    payload = {
        "body": issue.body,
        "labels": list(issue.labels),
        "state": issue.state,
        "title": issue.title,
        "updated_at": issue.updated_at,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class GitHubIssueIndexer:
    """Indexes and searches GitHub issues in the shared VectorStore."""

    def __init__(
        self,
        *,
        vector_store: VectorStoreProtocol | None,
        embed_fn: EmbedFnProtocol | None,
        similarity_threshold: float = 0.90,
    ) -> None:
        self.vector_store = vector_store
        self.embed_fn = embed_fn
        self.similarity_threshold = similarity_threshold

    async def upsert(self, issue: IssueSnapshot, *, task_id: str | None = None) -> str | None:
        """Embed and upsert an issue. Returns point id when indexed."""
        if self.vector_store is None or self.embed_fn is None:
            return None

        point_id = issue_point_id(issue.project_id, issue.repo, issue.issue_number)
        await self.vector_store.ensure_collection(GITHUB_ISSUE_COLLECTION)
        embedding = await self.embed_fn(build_issue_content(issue))
        await self.vector_store.upsert(
            point_id,
            embedding,
            {
                "project_id": issue.project_id,
                "repo": issue.repo,
                "issue_number": issue.issue_number,
                "issue_url": issue.issue_url,
                "state": issue.state,
                "labels": list(issue.labels),
                "updated_at": issue.updated_at,
                "content_hash": content_hash(issue),
                "task_id": task_id,
            },
            collection_name=GITHUB_ISSUE_COLLECTION,
        )
        return point_id

    async def find_duplicates(
        self,
        issue: IssueSnapshot,
        *,
        limit: int = 5,
    ) -> list[IssueDuplicate]:
        """Search project-scoped semantic duplicates, allowing cross-repo matches."""
        if self.vector_store is None or self.embed_fn is None:
            return []

        try:
            await self.vector_store.ensure_collection(GITHUB_ISSUE_COLLECTION)
            embedding = await self.embed_fn(build_issue_content(issue))
            results = await self.vector_store.search_with_payload(
                embedding,
                limit=limit + 1,
                filters={"project_id": issue.project_id},
                collection_name=GITHUB_ISSUE_COLLECTION,
            )
        except (
            VectorStoreUnavailableError,
            ResponseHandlingException,
            httpx.TransportError,
            RuntimeError,
            OSError,
        ):
            logger.warning(
                "GitHub issue vector duplicate search failed for %s; continuing without candidates",
                issue.issue_key,
                extra={
                    "project_id": issue.project_id,
                    "repo": issue.repo,
                    "issue_number": issue.issue_number,
                },
                exc_info=True,
            )
            return []

        duplicates: list[IssueDuplicate] = []
        for _point_id, score, payload in results:
            repo = str(payload.get("repo") or "")
            issue_number = payload.get("issue_number")
            if score < self.similarity_threshold or not repo or issue_number is None:
                continue
            try:
                parsed_issue_number = int(issue_number)
            except (TypeError, ValueError):
                continue
            if repo == issue.repo and parsed_issue_number == issue.issue_number:
                continue
            duplicates.append(
                IssueDuplicate(
                    repo=repo,
                    issue_number=parsed_issue_number,
                    issue_url=payload.get("issue_url"),
                    score=float(score),
                    task_id=payload.get("task_id"),
                )
            )
        return duplicates[:limit]
