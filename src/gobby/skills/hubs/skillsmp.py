"""SkillsMP provider implementation.

This module provides the SkillsMPProvider class which connects to the
SkillsMP REST API for skill search, listing, and download functionality.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from gobby.skills.hubs.base import DownloadResult, HubProvider, HubSkillDetails, HubSkillInfo
from gobby.skills.loader import GitHubRef, SkillLoadError, clone_skill_repo

logger = logging.getLogger(__name__)


class SkillsMPProvider(HubProvider):
    """Provider for SkillsMP skill marketplace using REST API.

    This provider connects to the SkillsMP API (skillsmp.com) to provide
    access to nearly 1M skills in the marketplace.

    Authentication is via Bearer token in the Authorization header.
    Rate limit: 500 requests/day.
    """

    def __init__(
        self,
        hub_name: str,
        base_url: str,
        auth_token: str | None = None,
    ) -> None:
        super().__init__(hub_name=hub_name, base_url=base_url, auth_token=auth_token)

    @property
    def provider_type(self) -> str:
        return "skillsmp"

    def _get_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def _require_auth(self) -> None:
        if not self.auth_token:
            raise RuntimeError(
                "SkillsMP API key not configured. "
                "Run 'gobby install' or 'gobby secrets set SKILLSMP_API_KEY'."
            )

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"

        async with httpx.AsyncClient() as client:
            try:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=self._get_headers(),
                    params=params,
                    timeout=30.0,
                )
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
            except httpx.HTTPStatusError as e:
                logger.error(f"SkillsMP API error: {e.response.status_code}")
                raise RuntimeError(f"SkillsMP API error: {e.response.status_code}") from e
            except httpx.RequestError as e:
                logger.error(f"SkillsMP request failed: {e}")
                raise RuntimeError(f"SkillsMP request failed: {e}") from e

    async def discover(self) -> dict[str, Any]:
        authenticated = self.auth_token is not None
        info: dict[str, Any] = {
            "hub_name": self.hub_name,
            "provider_type": self.provider_type,
            "base_url": self.base_url,
            "authenticated": authenticated,
        }
        if not authenticated:
            info["error"] = (
                "SKILLSMP_API_KEY not configured. "
                "Run 'gobby install' or 'gobby secrets set SKILLSMP_API_KEY'."
            )
        return info

    @staticmethod
    def _unwrap_skills(result: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract the skills array from SkillsMP's response envelope.

        The API wraps responses as ``{"success": bool, "data": {"skills": [...]}}``.
        Falls back to a top-level ``skills`` key for defensive forward compat.
        """
        data = result.get("data")
        if isinstance(data, dict) and "skills" in data:
            return list(data["skills"])
        # Defensive fallback for response shapes without the envelope.
        return list(result.get("skills", []))

    def _skill_to_info(self, skill: dict[str, Any]) -> HubSkillInfo:
        """Map a SkillsMP skill record to HubSkillInfo.

        SkillsMP uses ``stars`` as the popularity signal, surfaced as ``score``
        so the MCP layer can rank consistently with other hubs. ``version`` is
        not provided by the list/search endpoints.
        """
        stars = skill.get("stars")
        score = (
            float(stars) if isinstance(stars, int | float) and not isinstance(stars, bool) else None
        )
        return HubSkillInfo(
            slug=skill.get("id", skill.get("name", "")),
            display_name=skill.get("name", skill.get("id", "")),
            description=skill.get("description", ""),
            hub_name=self.hub_name,
            version=skill.get("version"),
            score=score if score is not None else skill.get("score"),
        )

    def _skill_to_details(self, skill: dict[str, Any]) -> HubSkillDetails:
        """Map a SkillsMP skill record to HubSkillDetails."""
        stars = skill.get("stars")
        score = (
            float(stars) if isinstance(stars, int | float) and not isinstance(stars, bool) else None
        )
        version = skill.get("version")
        versions_raw = skill.get("versions")
        versions = list(versions_raw) if isinstance(versions_raw, list) else []
        if not versions and version:
            versions = [version]
        return HubSkillDetails(
            slug=skill.get("id", skill.get("slug", skill.get("name", ""))),
            display_name=skill.get("name", skill.get("id", "")),
            description=skill.get("description", ""),
            hub_name=self.hub_name,
            version=version,
            score=score if score is not None else skill.get("score"),
            latest_version=skill.get("latest_version", version),
            versions=versions,
        )

    @staticmethod
    def _is_exact_match(skill: dict[str, Any], slug: str) -> bool:
        return skill.get("id") == slug or skill.get("slug") == slug

    @staticmethod
    def _search_queries_for_slug(slug: str) -> list[str]:
        queries = [slug]
        base = slug.removesuffix("-skill-md")

        for marker in ("-skills-", "-skill-", "-plugins-", "-library-"):
            if marker in base:
                queries.append(base.rsplit(marker, 1)[1])

        parts = base.split("-")
        preferred_suffix_widths = [3, 2, 4, 5, 1, 6, 7, 8]
        for width in preferred_suffix_widths:
            if len(parts) >= width:
                queries.append("-".join(parts[-width:]))

        return list(dict.fromkeys(query for query in queries if query))

    async def _search_exact_skill(self, slug: str) -> dict[str, Any] | None:
        for query in self._search_queries_for_slug(slug):
            result = await self._make_request(
                method="GET",
                endpoint="/skills/search",
                params={"q": query, "limit": 10},
            )

            for skill in self._unwrap_skills(result):
                if self._is_exact_match(skill, slug):
                    return skill
        return None

    async def search(
        self,
        query: str,
        limit: int = 20,
    ) -> list[HubSkillInfo]:
        self._require_auth()

        result = await self._make_request(
            method="GET",
            endpoint="/skills/search",
            params={"q": query, "limit": limit},
        )

        return [self._skill_to_info(skill) for skill in self._unwrap_skills(result)]

    async def list_skills(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[HubSkillInfo]:
        """List skills via /skills/search with a broad match.

        SkillsMP has no dedicated unfiltered-list endpoint — /skills returns
        404. Route through /skills/search with an empty query so pagination
        (via limit/offset) still works for callers that want a browse view.
        """
        self._require_auth()
        # Coerce non-positive limits to a sane browse default. Callers that pass
        # limit=0 mean "browse mode" — the API still needs a positive limit for
        # pagination to work, and forwarding 0 (or negative) yields a 400.
        safe_limit = limit if limit > 0 else 50
        page = (offset // safe_limit) + 1

        result = await self._make_request(
            method="GET",
            endpoint="/skills/search",
            params={"q": "", "limit": safe_limit, "page": page},
        )

        return [self._skill_to_info(skill) for skill in self._unwrap_skills(result)]

    async def get_skill_details(
        self,
        slug: str,
    ) -> HubSkillDetails | None:
        self._require_auth()
        try:
            skill = await self._search_exact_skill(slug)
        except RuntimeError:
            return None

        if skill is None:
            return None
        return self._skill_to_details(skill)

    async def download_skill(
        self,
        slug: str,
        version: str | None = None,
        target_dir: str | None = None,
    ) -> DownloadResult:
        try:
            self._require_auth()
            skill = await self._search_exact_skill(slug)
            if skill is None:
                return DownloadResult(success=False, slug=slug, error=f"Skill not found: {slug}")

            github_url = skill.get("githubUrl")
            if not isinstance(github_url, str) or not github_url.strip():
                return DownloadResult(
                    success=False,
                    slug=slug,
                    error="No GitHub source URL provided",
                )

            path, ref = self._download_from_github(github_url.strip(), version, target_dir)
            return DownloadResult(
                success=True,
                slug=slug,
                path=path,
                version=version or ref.branch,
            )
        except (RuntimeError, SkillLoadError, OSError, ValueError) as e:
            logger.error(f"Failed to download SkillsMP skill {slug}: {e}")
            return DownloadResult(success=False, slug=slug, error=str(e))

    def _download_from_github(
        self,
        github_url: str,
        version: str | None,
        target_dir: str | None,
    ) -> tuple[str, GitHubRef]:
        ref = self._parse_github_url(github_url, version)
        repo_path = clone_skill_repo(ref)
        skill_path = repo_path / ref.path if ref.path else repo_path
        self._validate_skill_directory(skill_path)
        return self._copy_skill_directory(skill_path, target_dir), ref

    @classmethod
    def _parse_github_url(cls, github_url: str, version: str | None = None) -> GitHubRef:
        parsed = urlparse(github_url)
        host = parsed.netloc.lower()
        parts = [unquote(part) for part in parsed.path.split("/") if part]

        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"Unsupported GitHub URL scheme: {parsed.scheme}")

        if host == "github.com":
            return cls._parse_github_com_url(parts, version)
        if host == "raw.githubusercontent.com":
            return cls._parse_raw_github_url(parts, version)

        raise ValueError(f"Unsupported GitHub source URL: {github_url}")

    @classmethod
    def _parse_github_com_url(cls, parts: list[str], version: str | None) -> GitHubRef:
        if len(parts) < 2:
            raise ValueError("GitHub URL must include owner and repository")

        owner = parts[0]
        repo = parts[1].removesuffix(".git")
        tail = parts[2:]

        if not tail:
            return GitHubRef(owner=owner, repo=repo, branch=version)

        if tail[0] == "tree":
            if len(tail) < 2:
                raise ValueError("GitHub tree URL must include a branch")
            branch = version or tail[1]
            path = cls._join_safe_path(tail[2:])
            return GitHubRef(owner=owner, repo=repo, branch=branch, path=path)

        if tail[0] == "blob":
            if len(tail) < 3:
                raise ValueError("GitHub blob URL must point to SKILL.md")
            branch = version or tail[1]
            path = cls._skill_directory_from_file_parts(tail[2:])
            return GitHubRef(owner=owner, repo=repo, branch=branch, path=path)

        if tail[-1] == "SKILL.md":
            path = cls._skill_directory_from_file_parts(tail)
            return GitHubRef(owner=owner, repo=repo, branch=version, path=path)

        raise ValueError("Unsupported GitHub source URL shape")

    @classmethod
    def _parse_raw_github_url(cls, parts: list[str], version: str | None) -> GitHubRef:
        if len(parts) < 4:
            raise ValueError("Raw GitHub URL must point to SKILL.md")

        owner = parts[0]
        repo = parts[1].removesuffix(".git")
        branch = version or parts[2]
        path = cls._skill_directory_from_file_parts(parts[3:])
        return GitHubRef(owner=owner, repo=repo, branch=branch, path=path)

    @classmethod
    def _skill_directory_from_file_parts(cls, parts: list[str]) -> str | None:
        if not parts or parts[-1] != "SKILL.md":
            raise ValueError("GitHub source URL must point to SKILL.md")
        return cls._join_safe_path(parts[:-1])

    @staticmethod
    def _join_safe_path(parts: list[str]) -> str | None:
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("GitHub source URL contains an unsafe path")
        return "/".join(parts) or None

    @staticmethod
    def _validate_skill_directory(skill_path: Path) -> None:
        if not skill_path.is_dir():
            raise RuntimeError(f"Skill path is not a directory: {skill_path}")
        if not (skill_path / "SKILL.md").is_file():
            raise RuntimeError(f"SKILL.md not found in GitHub source: {skill_path}")

    @staticmethod
    def _copy_skill_directory(skill_path: Path, target_dir: str | None) -> str:
        if target_dir is None:
            return str(skill_path)

        target = Path(target_dir)
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.copytree(skill_path, target)
        return str(target)
