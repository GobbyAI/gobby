"""SHA-pinned GitHub topic skill hub provider."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import tarfile
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

import httpx

from gobby.skills.hubs.base import DownloadResult, HubProvider, HubSkillDetails, HubSkillInfo

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
MAX_SEARCH_PAGES = 3
MAX_REPOS = 100
MAX_CONCURRENT_PROBES = 4
MAX_TREE_ENTRIES = 200
MAX_TREE_DEPTH = 3
MAX_SKILLS_PER_REPO = 50
MAX_SKILL_FILE_BYTES = 256 * 1024
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 512
MAX_EXPANDED_BYTES = 40 * 1024 * 1024
MAX_EXPANSION_RATIO = 100
MIN_RATIO_CHECK_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30.0

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class TopicHubError(ValueError):
    """A GitHub topic hub boundary rejected external data."""


class ItemUnavailable(TopicHubError):
    """A pinned discovery record or its GitHub object is unavailable."""


class RateLimited(TopicHubError):
    """GitHub rate-limited discovery refresh."""


@dataclass(frozen=True)
class DiscoveryRecord:
    item_id: str
    repo: str
    path: str
    sha: str

    def to_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "repo": self.repo,
            "path": self.path,
            "sha": self.sha,
        }


@dataclass(frozen=True)
class RepoCandidate:
    repo: str
    default_branch: str


def normalize_topic_item_id(item_id: str) -> str:
    """Validate and return canonical ``owner/repo:path`` identity."""
    if item_id.count(":") != 1:
        raise TopicHubError("invalid GitHub topic item ID")
    repo, raw_path = item_id.split(":", 1)
    if repo.count("/") != 1:
        raise TopicHubError("invalid GitHub topic repository")
    owner, repo_name = repo.split("/", 1)
    if not _OWNER_RE.fullmatch(owner) or not _REPO_RE.fullmatch(repo_name):
        raise TopicHubError("invalid GitHub topic repository")
    if not raw_path or raw_path.startswith(("/", "\\")) or "\\" in raw_path:
        raise TopicHubError("invalid GitHub topic path")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw_path):
        raise TopicHubError("invalid GitHub topic path")
    raw_parts = raw_path.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in raw_parts):
        raise TopicHubError("invalid GitHub topic path")
    path = PurePosixPath(*raw_parts)
    if path.is_absolute() or ".." in path.parts:
        raise TopicHubError("invalid GitHub topic path")
    return f"{owner}/{repo_name}:{path.as_posix()}"


def _parse_description(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("description:"):
            return line.partition(":")[2].strip().strip('"').strip("'")
    return ""


def _safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise TopicHubError("unsafe archive path")
    return path


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _extract_skill_archive(data: bytes, record: DiscoveryRecord, target_dir: str | None) -> str:
    stage = Path(tempfile.mkdtemp(prefix="gobby-topic-"))
    target = Path(target_dir) if target_dir is not None else None
    target_touched = False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise TopicHubError("archive member cap exceeded")
            expanded_bytes = sum(member.size for member in members if member.isfile())
            if expanded_bytes > MAX_EXPANDED_BYTES:
                raise TopicHubError("archive expansion cap exceeded")
            if (
                expanded_bytes > MIN_RATIO_CHECK_BYTES
                and expanded_bytes > len(data) * MAX_EXPANSION_RATIO
            ):
                raise TopicHubError("archive expansion ratio exceeded")

            safe_members: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
            roots: set[str] = set()
            for member in members:
                if not (member.isdir() or member.isfile()):
                    raise TopicHubError("archive contains unsupported member type")
                member_path = _safe_archive_path(member.name)
                roots.add(member_path.parts[0])
                safe_members.append((member, member_path))
            if len(roots) != 1:
                raise TopicHubError("archive root is ambiguous")

            prefix = PurePosixPath(next(iter(roots))) / PurePosixPath(record.path)
            extracted_files = 0
            for member, member_path in safe_members:
                try:
                    relative = member_path.relative_to(prefix)
                except ValueError:
                    continue
                if not relative.parts:
                    continue
                destination = stage.joinpath(*relative.parts)
                destination_resolved = destination.resolve()
                if stage.resolve() not in destination_resolved.parents:
                    raise TopicHubError("archive extraction escaped destination")
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if relative.name == "SKILL.md" and member.size > MAX_SKILL_FILE_BYTES:
                    raise TopicHubError("SKILL.md size cap exceeded")
                source = archive.extractfile(member)
                if source is None:
                    raise TopicHubError("archive file could not be read")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                extracted_files += 1

        if extracted_files == 0 or not (stage / "SKILL.md").is_file():
            raise ItemUnavailable("item_unavailable")
        if target_dir is None:
            return str(stage)

        assert target is not None
        target.parent.mkdir(parents=True, exist_ok=True)
        _remove_path(target)
        target_touched = True
        shutil.move(str(stage), str(target))
        return str(target)
    except (OSError, tarfile.TarError, TopicHubError):
        _remove_path(stage)
        if target_touched and target is not None:
            _remove_path(target)
        raise


class GitHubTopicProvider(HubProvider):
    """Discover public skill repositories by topic and pin every item to one SHA."""

    def __init__(
        self,
        hub_name: str,
        base_url: str,
        auth_token: str | None = None,
        topic: str = "gobby-skill",
        cache_ttl_seconds: int = 1800,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(hub_name, base_url or GITHUB_API, auth_token)
        if cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be positive")
        self._topic = topic
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        self._sleep = sleep
        self._records: dict[str, DiscoveryRecord] = {}
        self._details_cache: dict[tuple[str, str], HubSkillDetails] = {}
        self._cache_expires_at = 0.0
        self._probe_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROBES)
        self._refresh_lock = asyncio.Lock()

    @property
    def provider_type(self) -> str:
        return "github-topic"

    @property
    def topic(self) -> str:
        return self._topic

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    async def _get(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> httpx.Response:
        return await client.get(
            f"{self.base_url.rstrip('/')}/{path.lstrip('/')}",
            headers=self._headers(accept),
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    async def _search_page(
        self,
        client: httpx.AsyncClient,
        page: int,
    ) -> list[RepoCandidate]:
        response: httpx.Response | None = None
        for attempt in range(3):
            response = await self._get(
                client,
                "/search/repositories",
                params={"q": f"topic:{self.topic}", "per_page": 100, "page": page},
            )
            if response.status_code != 403:
                break
            if attempt < 2:
                await self._sleep(float(2**attempt))
        if response is None or response.status_code == 403:
            raise RateLimited("GitHub topic discovery rate limited")
        response.raise_for_status()
        payload = cast(dict[str, object], response.json())
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            return []
        candidates: list[RepoCandidate] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            repo = raw_item.get("full_name")
            branch = raw_item.get("default_branch")
            if isinstance(repo, str) and isinstance(branch, str):
                candidates.append(RepoCandidate(repo=repo, default_branch=branch))
        return candidates

    async def _probe_repo(
        self,
        client: httpx.AsyncClient,
        candidate: RepoCandidate,
    ) -> list[DiscoveryRecord]:
        async with self._probe_semaphore:
            try:
                commit = await self._get(
                    client, f"/repos/{candidate.repo}/commits/{candidate.default_branch}"
                )
                commit.raise_for_status()
                commit_payload = commit.json()
                if not isinstance(commit_payload, dict):
                    raise TopicHubError("repository returned invalid commit payload")
                sha = commit_payload.get("sha")
                if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
                    raise TopicHubError("repository returned invalid commit SHA")

                tree_response = await self._get(
                    client, f"/repos/{candidate.repo}/git/trees/{sha}", params={"recursive": 1}
                )
                tree_response.raise_for_status()
                tree_payload = tree_response.json()
                if not isinstance(tree_payload, dict):
                    raise TopicHubError("repository returned invalid tree payload")
                raw_tree = tree_payload.get("tree")
                if tree_payload.get("truncated") is True or not isinstance(raw_tree, list):
                    logger.warning("Skipping capped topic repository %s", candidate.repo)
                    return []
                if len(raw_tree) > MAX_TREE_ENTRIES:
                    logger.warning("Skipping oversized topic repository %s", candidate.repo)
                    return []

                records: list[DiscoveryRecord] = []
                for entry in raw_tree:
                    if not isinstance(entry, dict) or entry.get("type") != "blob":
                        continue
                    raw_path = entry.get("path")
                    raw_size = entry.get("size")
                    if not isinstance(raw_path, str) or not raw_path.endswith("/SKILL.md"):
                        continue
                    if not isinstance(raw_size, int) or raw_size > MAX_SKILL_FILE_BYTES:
                        continue
                    skill_path = raw_path.removesuffix("/SKILL.md")
                    if len(PurePosixPath(skill_path).parts) > MAX_TREE_DEPTH:
                        continue
                    item_id = normalize_topic_item_id(f"{candidate.repo}:{skill_path}")
                    records.append(
                        DiscoveryRecord(
                            item_id=item_id,
                            repo=candidate.repo,
                            path=skill_path,
                            sha=sha.lower(),
                        )
                    )
                    if len(records) > MAX_SKILLS_PER_REPO:
                        logger.warning(
                            "Skipping topic repository %s with over %s skills",
                            candidate.repo,
                            MAX_SKILLS_PER_REPO,
                        )
                        return []
                return records
            except (httpx.HTTPError, TopicHubError, ValueError) as exc:
                logger.warning("Skipping GitHub topic repository %s: %s", candidate.repo, exc)
                return []

    def _payload(self) -> dict[str, object]:
        return {
            "hub_name": self.hub_name,
            "provider_type": self.provider_type,
            "topic": self.topic,
            "authenticated": self.auth_token is not None,
            "records": [record.to_dict() for record in self._records.values()],
        }

    async def discover(self) -> dict[str, object]:
        if self._records and self._clock() < self._cache_expires_at:
            return self._payload()
        async with self._refresh_lock:
            if self._records and self._clock() < self._cache_expires_at:
                return self._payload()
            return await self._refresh()

    async def _refresh(self) -> dict[str, object]:
        try:
            candidates: list[RepoCandidate] = []
            async with httpx.AsyncClient() as client:
                for page in range(1, MAX_SEARCH_PAGES + 1):
                    page_candidates = await self._search_page(client, page)
                    candidates.extend(page_candidates[: MAX_REPOS - len(candidates)])
                    if len(page_candidates) < 100 or len(candidates) >= MAX_REPOS:
                        break

                tasks: list[asyncio.Task[list[DiscoveryRecord]]] = []
                async with asyncio.TaskGroup() as group:
                    for candidate in candidates:
                        tasks.append(group.create_task(self._probe_repo(client, candidate)))
            records = [record for task in tasks for record in task.result()]
            self._records = {record.item_id: record for record in records}
            self._details_cache = {
                key: details
                for key, details in self._details_cache.items()
                if key[0] in self._records and self._records[key[0]].sha == key[1]
            }
            self._cache_expires_at = self._clock() + self._cache_ttl_seconds
            return self._payload()
        except RateLimited:
            if self._records:
                logger.warning("GitHub topic refresh rate limited; serving cached discovery")
                return self._payload()
            raise

    async def _ensure_discovered(self) -> None:
        if not self._records:
            await self.discover()

    async def list_skills(self, limit: int = 50, offset: int = 0) -> list[HubSkillInfo]:
        await self._ensure_discovered()
        skills = [
            HubSkillInfo(
                slug=record.item_id,
                display_name=PurePosixPath(record.path).name,
                description="",
                hub_name=self.hub_name,
                version=record.sha,
            )
            for record in self._records.values()
        ]
        return skills[offset : offset + limit]

    async def search(self, query: str, limit: int = 20) -> list[HubSkillInfo]:
        skills = await self.list_skills(limit=MAX_REPOS * MAX_SKILLS_PER_REPO)
        needle = query.casefold()
        return [
            skill
            for skill in skills
            if needle in skill.slug.casefold() or needle in skill.display_name.casefold()
        ][:limit]

    def _record_for(self, item_id: str) -> DiscoveryRecord:
        try:
            canonical = normalize_topic_item_id(item_id)
        except TopicHubError as exc:
            raise ItemUnavailable("item_unavailable") from exc
        record = self._records.get(canonical)
        if record is None or record.item_id != canonical:
            raise ItemUnavailable("item_unavailable")
        if normalize_topic_item_id(f"{record.repo}:{record.path}") != record.item_id:
            raise ItemUnavailable("item_unavailable")
        if not _SHA_RE.fullmatch(record.sha):
            raise ItemUnavailable("item_unavailable")
        return record

    async def get_skill_details(self, slug: str) -> HubSkillDetails | None:
        await self._ensure_discovered()
        try:
            record = self._record_for(slug)
            cache_key = (record.item_id, record.sha)
            cached = self._details_cache.get(cache_key)
            if cached is not None:
                return cached
            async with httpx.AsyncClient() as client:
                response = await self._get(
                    client,
                    f"/repos/{record.repo}/contents/{record.path}/SKILL.md",
                    params={"ref": record.sha},
                    accept="application/vnd.github.raw+json",
                )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            if len(response.content) > MAX_SKILL_FILE_BYTES:
                raise ItemUnavailable("item_unavailable")
            content = response.content.decode("utf-8")
            name = PurePosixPath(record.path).name
            details = HubSkillDetails(
                slug=record.item_id,
                display_name=name,
                description=_parse_description(content),
                hub_name=self.hub_name,
                version=record.sha,
                latest_version=record.sha,
                versions=[record.sha],
            )
            self._details_cache[cache_key] = details
            return details
        except (httpx.HTTPError, UnicodeDecodeError, ItemUnavailable):
            return None

    async def _download_archive(self, record: DiscoveryRecord) -> bytes:
        url = f"{self.base_url.rstrip('/')}/repos/{record.repo}/tarball/{record.sha}"
        chunks: list[bytes] = []
        size = 0
        async with httpx.AsyncClient(follow_redirects=True) as client:
            async with client.stream(
                "GET",
                url,
                headers=self._headers("application/vnd.github+json"),
                timeout=REQUEST_TIMEOUT_SECONDS,
            ) as response:
                if response.status_code == 404:
                    raise ItemUnavailable("item_unavailable")
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise TopicHubError("compressed archive cap exceeded")
                    chunks.append(chunk)
        return b"".join(chunks)

    async def download_skill(
        self,
        slug: str,
        version: str | None = None,
        target_dir: str | None = None,
    ) -> DownloadResult:
        try:
            await self._ensure_discovered()
            record = self._record_for(slug)
            if version is not None and version != record.sha:
                raise ItemUnavailable("item_unavailable")
            archive = await self._download_archive(record)
            path = await asyncio.to_thread(_extract_skill_archive, archive, record, target_dir)
            return DownloadResult(
                success=True,
                slug=record.item_id,
                path=path,
                version=record.sha,
                is_temp=target_dir is None,
                provenance=record.to_dict(),
            )
        except (httpx.HTTPError, OSError, tarfile.TarError, TopicHubError) as exc:
            error = "item_unavailable" if isinstance(exc, ItemUnavailable) else str(exc)
            logger.warning("GitHub topic download failed for %s: %s", slug, error)
            return DownloadResult(success=False, slug=slug, error=error)
