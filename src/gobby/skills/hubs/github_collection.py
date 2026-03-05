"""GitHub Collection provider implementation.

This module provides the GitHubCollectionProvider class which provides
access to skill collections hosted in GitHub repositories.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from gobby.skills.hubs.base import DownloadResult, HubProvider, HubSkillDetails, HubSkillInfo
from gobby.skills.loader import GitHubRef, clone_skill_repo

if TYPE_CHECKING:
    from gobby.llm.service import LLMService

logger = logging.getLogger(__name__)


class GitHubCollectionProvider(HubProvider):
    """Provider for GitHub-hosted skill collections.

    This provider accesses skills stored in a GitHub repository,
    typically organized as a collection of skill directories.

    The repository structure is expected to be:
    ```
    repo/
    ├── skill-1/
    │   └── SKILL.md
    ├── skill-2/
    │   └── SKILL.md
    └── ...
    ```

    Example usage:
        ```python
        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",  # Not used for GitHub
            repo="user/my-skills",
            branch="main",
            auth_token="ghp_your_token",  # Optional, for private repos
        )

        skills = await provider.list_skills()
        for skill in skills:
            print(f"{skill.slug}: {skill.description}")
        ```
    """

    # Cache TTL for synthesized descriptions (1 hour)
    CACHE_TTL = 3600

    def __init__(
        self,
        hub_name: str,
        base_url: str,
        repo: str | None = None,
        branch: str = "main",
        path: str | None = None,
        auth_token: str | None = None,
        llm_service: LLMService | None = None,
        config: Any | None = None,
    ) -> None:
        """Initialize the GitHub Collection provider.

        Args:
            hub_name: The configured name for this hub instance
            base_url: Not used for GitHub (kept for interface compatibility)
            repo: GitHub repository in 'owner/repo' format
            branch: Git branch to use (default: 'main')
            path: Subdirectory path within repo where skills are located
            auth_token: Optional GitHub token for private repos
            llm_service: Optional LLM service for synthesizing descriptions
            config: Optional SkillDescriptionConfig for provider/model selection
        """
        super().__init__(hub_name=hub_name, base_url=base_url, auth_token=auth_token)
        self._repo = repo or ""
        self._branch = branch
        self._path = path
        self._llm_service = llm_service
        self._config = config
        self._description_cache: dict[str, tuple[str, float]] = {}

    @property
    def provider_type(self) -> str:
        """Return the provider type identifier."""
        return "github-collection"

    @property
    def repo(self) -> str:
        """GitHub repository in 'owner/repo' format."""
        return self._repo

    @property
    def branch(self) -> str:
        """Git branch to use."""
        return self._branch

    @property
    def path(self) -> str | None:
        """Subdirectory path within repo where skills are located."""
        return self._path

    async def _fetch_skill_list(self) -> list[dict[str, Any]]:
        """Fetch the list of skills from the repository.

        Uses the GitHub API to list contents of the repository root,
        filtering for directories which represent skills.

        Returns:
            List of skill metadata dictionaries with 'slug', 'name' keys
        """
        if not self._repo or "/" not in self._repo:
            logger.warning(f"Invalid repo format: {self._repo}")
            return []

        owner, repo = self._repo.split("/", 1)
        url = f"https://api.github.com/repos/{owner}/{repo}/contents"
        if self._path:
            url = f"{url}/{self._path.strip('/')}"

        headers: dict[str, str] = {
            "Accept": "application/vnd.github.v3+json",
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        params: dict[str, str] = {}
        if self._branch:
            params["ref"] = self._branch

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=30.0,
                )
                response.raise_for_status()
                contents: list[dict[str, Any]] = response.json()

            # Filter for directories only (skills are directories)
            # Support nested layouts: if a top-level dir has no SKILL.md,
            # recurse one level to find subdirs that are actual skills.
            skills = []
            for item in contents:
                if item.get("type") == "dir":
                    name = item.get("name", "")
                    if name.startswith("."):
                        continue
                    skills.append(
                        {
                            "slug": name,
                            "name": name,
                            "description": "",
                            "_url": item.get("url", ""),
                        }
                    )

            # Check for nested structure: probe first dir for SKILL.md
            if skills and skills[0].get("_url"):
                has_skill_md = await self._dir_has_skill_md(skills[0]["slug"], headers, params)
                if not has_skill_md:
                    # Nested: each top-level dir is a category, recurse
                    nested_skills = []
                    for category_item in skills:
                        sub_items = await self._fetch_subdir_skills(
                            category_item["slug"], headers, params
                        )
                        nested_skills.extend(sub_items)
                    skills = nested_skills

            # Strip internal _url key
            for s in skills:
                s.pop("_url", None)

            return skills

        except httpx.HTTPStatusError as e:
            logger.error(f"GitHub API error: {e.response.status_code} for {url}")
            return []
        except httpx.RequestError as e:
            logger.error(f"GitHub API request failed: {e}")
            return []

    async def _dir_has_skill_md(
        self,
        dir_name: str,
        headers: dict[str, str],
        params: dict[str, str],
    ) -> bool:
        """Check if a directory contains SKILL.md."""
        if not self._repo or "/" not in self._repo:
            return False

        owner, repo = self._repo.split("/", 1)
        skill_path = f"{self._path.strip('/')}/{dir_name}" if self._path else dir_name
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{skill_path}/SKILL.md"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.head(url, headers=headers, params=params, timeout=10.0)
                return response.status_code == 200
        except httpx.RequestError:
            return False

    async def _fetch_subdir_skills(
        self,
        category_name: str,
        headers: dict[str, str],
        params: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Fetch skill directories from a category subdirectory."""
        if not self._repo or "/" not in self._repo:
            return []

        owner, repo = self._repo.split("/", 1)
        sub_path = f"{self._path.strip('/')}/{category_name}" if self._path else category_name
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{sub_path}"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, params=params, timeout=30.0)
                response.raise_for_status()
                contents: list[dict[str, Any]] = response.json()

            skills = []
            for item in contents:
                if item.get("type") == "dir":
                    name = item.get("name", "")
                    if name.startswith("."):
                        continue
                    skills.append(
                        {
                            "slug": f"{category_name}/{name}",
                            "name": name,
                            "description": "",
                        }
                    )
            return skills
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.debug(f"Failed to fetch subdirectory {category_name}: {e}")
            return []

    async def _fetch_skill_content(self, slug: str) -> str | None:
        """Fetch SKILL.md content for a skill.

        Args:
            slug: The skill's directory name

        Returns:
            SKILL.md content as string, or None if not found
        """
        if not self._repo or "/" not in self._repo:
            return None

        owner, repo = self._repo.split("/", 1)
        skill_path = f"{self._path.strip('/')}/{slug}" if self._path else slug
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{skill_path}/SKILL.md"

        headers: dict[str, str] = {
            "Accept": "application/vnd.github.v3.raw",  # Get raw content
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        params: dict[str, str] = {}
        if self._branch:
            params["ref"] = self._branch

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=30.0,
                )
                response.raise_for_status()
                return response.text
        except httpx.HTTPStatusError as e:
            logger.debug(f"Could not fetch SKILL.md for {slug}: {e.response.status_code}")
            return None
        except httpx.RequestError as e:
            logger.debug(f"Request failed fetching SKILL.md for {slug}: {e}")
            return None

    async def _synthesize_description(self, slug: str, content: str) -> str:
        """Use LLM to synthesize a concise description from SKILL.md content.

        Args:
            slug: The skill's directory name (for context)
            content: SKILL.md content

        Returns:
            Synthesized description string (empty if LLM unavailable)
        """
        if not self._llm_service:
            return ""

        # Truncate content to fit in prompt
        snippet = content[:1500]

        prompt = f"""Generate a concise 1-sentence description (max 100 chars) for this skill.

Skill name: {slug}

SKILL.md content:
{snippet}

Output ONLY the description text, no quotes, no explanation, no preamble."""

        try:
            if self._config:
                try:
                    provider, model, _ = self._llm_service.get_provider_for_feature(self._config)
                except (ValueError, Exception):
                    provider = self._llm_service.get_default_provider()
                    model = None
            else:
                provider = self._llm_service.get_default_provider()
                model = None
            description = await provider.generate_text(prompt, model=model)
            # Clean up LLM output
            return description.strip().strip('"').strip("'")[:100]
        except Exception as e:
            logger.warning(f"Failed to synthesize description for {slug}: {e}")
            return ""

    async def _clone_skill(
        self,
        slug: str,
        target_dir: str | None = None,
        version: str | None = None,
    ) -> str:
        """Clone a specific skill from the repository.

        Uses clone_skill_repo to clone the entire repository, then
        returns the path to the specific skill directory within it.
        If target_dir is specified, copies the skill directory there.

        Args:
            slug: The skill's directory name
            target_dir: Optional target directory to copy skill to
            version: Optional version/branch to checkout (overrides default branch)

        Returns:
            Path to the skill directory
        """
        # Parse repo into owner/repo
        if "/" not in self._repo:
            raise ValueError(f"Invalid repo format: {self._repo}, expected owner/repo")

        owner, repo = self._repo.split("/", 1)

        # Build skill path within repo (accounting for subdirectory)
        skill_subpath = f"{self._path.strip('/')}/{slug}" if self._path else slug

        # Build GitHubRef with optional version override
        ref = GitHubRef(
            owner=owner,
            repo=repo,
            branch=version or self._branch,
            path=skill_subpath,
        )

        # Clone/update the repository
        repo_path = clone_skill_repo(ref)

        # Path to the skill within the repo
        skill_path = repo_path / skill_subpath

        # If target_dir specified, copy skill there
        if target_dir:
            target = Path(target_dir)
            if skill_path.exists():
                # Copy skill directory contents to target
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(skill_path, target)
            return target_dir

        return str(skill_path)

    async def discover(self) -> dict[str, Any]:
        """Discover hub capabilities.

        Returns:
            Dictionary with hub info
        """
        return {
            "hub_name": self.hub_name,
            "provider_type": self.provider_type,
            "repo": self.repo,
            "branch": self.branch,
            "path": self.path,
            "authenticated": self.auth_token is not None,
        }

    async def search(
        self,
        query: str,
        limit: int = 20,
    ) -> list[HubSkillInfo]:
        """Search for skills matching a query.

        This performs client-side filtering of the skill list.

        Args:
            query: Search query string
            limit: Maximum number of results

        Returns:
            List of matching skills with basic info
        """
        # Get all skills and filter locally
        all_skills = await self.list_skills(limit=1000)

        query_lower = query.lower()
        matching = [
            skill
            for skill in all_skills
            if query_lower in skill.slug.lower()
            or query_lower in skill.display_name.lower()
            or query_lower in skill.description.lower()
        ]

        return matching[:limit]

    async def list_skills(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[HubSkillInfo]:
        """List available skills from the repository.

        Args:
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of skills with basic info
        """
        skills_data = await self._fetch_skill_list()

        skills = [
            HubSkillInfo(
                slug=skill.get("slug", skill.get("name", "")),
                display_name=skill.get("name", skill.get("slug", "")),
                description=skill.get("description", ""),
                hub_name=self.hub_name,
                version=skill.get("version"),
            )
            for skill in skills_data
        ]

        return skills[offset : offset + limit]

    async def get_skill_details(
        self,
        slug: str,
    ) -> HubSkillDetails | None:
        """Get detailed information about a specific skill.

        Fetches SKILL.md content and uses LLM to synthesize a concise
        description. Results are cached for CACHE_TTL seconds.

        Args:
            slug: The skill's unique identifier

        Returns:
            Detailed skill info with synthesized description, or None if not found
        """
        # Verify skill exists in the list
        all_skills = await self.list_skills(limit=1000)
        skill_exists = any(skill.slug == slug for skill in all_skills)
        if not skill_exists:
            return None

        # Check cache first
        cache_key = f"{self._repo}:{slug}"
        if cache_key in self._description_cache:
            cached_desc, cached_at = self._description_cache[cache_key]
            if time.time() - cached_at < self.CACHE_TTL:
                return HubSkillDetails(
                    slug=slug,
                    display_name=slug,
                    description=cached_desc,
                    hub_name=self.hub_name,
                    version=self._branch,
                    latest_version=self._branch,
                    versions=[self._branch],
                )

        # Fetch SKILL.md content
        content = await self._fetch_skill_content(slug)
        description = ""

        if content:
            # Synthesize description using LLM
            description = await self._synthesize_description(slug, content)
            # Cache the result
            self._description_cache[cache_key] = (description, time.time())

        return HubSkillDetails(
            slug=slug,
            display_name=slug,
            description=description,
            hub_name=self.hub_name,
            version=self._branch,
            latest_version=self._branch,
            versions=[self._branch],
        )

    async def download_skill(
        self,
        slug: str,
        version: str | None = None,
        target_dir: str | None = None,
    ) -> DownloadResult:
        """Download and extract a skill from the repository.

        Args:
            slug: The skill's unique identifier
            version: Specific version (branch/tag) to download
            target_dir: Directory to extract to

        Returns:
            DownloadResult with success status, path, version, or error
        """
        try:
            path = await self._clone_skill(slug, target_dir, version)
            return DownloadResult(
                success=True,
                slug=slug,
                path=path,
                version=version or self.branch,
            )
        except Exception as e:
            logger.error(f"Failed to download skill {slug}: {e}")
            return DownloadResult(
                success=False,
                slug=slug,
                error=str(e),
            )
