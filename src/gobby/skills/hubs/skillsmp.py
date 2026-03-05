"""SkillsMP provider implementation.

This module provides the SkillsMPProvider class which connects to the
SkillsMP REST API for skill search, listing, and download functionality.
"""

from __future__ import annotations

import logging
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

from gobby.skills.hubs.base import DownloadResult, HubProvider, HubSkillDetails, HubSkillInfo

logger = logging.getLogger(__name__)


class SkillsMPProvider(HubProvider):
    """Provider for SkillsMP skill marketplace using REST API.

    This provider connects to the SkillsMP API (skillsmp.com) to provide
    access to 350K+ skills in the marketplace.

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
            info["error"] = "SKILLSMP_API_KEY not set. Search and listing require authentication."
        return info

    async def search(
        self,
        query: str,
        limit: int = 20,
    ) -> list[HubSkillInfo]:
        if not self.auth_token:
            raise RuntimeError(
                "SkillsMP API key not configured. Set the SKILLSMP_API_KEY environment variable."
            )

        result = await self._make_request(
            method="GET",
            endpoint="/skills/search",
            params={"q": query, "limit": limit},
        )

        skills = result.get("skills", [])
        return [
            HubSkillInfo(
                slug=skill.get("id", skill.get("name", "")),
                display_name=skill.get("name", skill.get("id", "")),
                description=skill.get("description", ""),
                hub_name=self.hub_name,
                version=skill.get("version"),
                score=skill.get("score"),
            )
            for skill in skills
        ]

    async def list_skills(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[HubSkillInfo]:
        if not self.auth_token:
            raise RuntimeError(
                "SkillsMP API key not configured. Set the SKILLSMP_API_KEY environment variable."
            )

        result = await self._make_request(
            method="GET",
            endpoint="/skills",
            params={"limit": limit, "offset": offset},
        )

        skills = result.get("skills", [])
        return [
            HubSkillInfo(
                slug=skill.get("id", skill.get("name", "")),
                display_name=skill.get("name", skill.get("id", "")),
                description=skill.get("description", ""),
                hub_name=self.hub_name,
                version=skill.get("version"),
            )
            for skill in skills
        ]

    async def get_skill_details(
        self,
        slug: str,
    ) -> HubSkillDetails | None:
        try:
            result = await self._make_request(
                method="GET",
                endpoint=f"/skills/{slug}",
            )

            if not result:
                return None

            return HubSkillDetails(
                slug=result.get("id", slug),
                display_name=result.get("name", slug),
                description=result.get("description", ""),
                hub_name=self.hub_name,
                version=result.get("version"),
                latest_version=result.get("latest_version", result.get("version")),
                versions=result.get("versions", []),
            )
        except RuntimeError:
            return None

    async def download_skill(
        self,
        slug: str,
        version: str | None = None,
        target_dir: str | None = None,
    ) -> DownloadResult:
        params: dict[str, Any] = {}
        if version:
            params["version"] = version

        try:
            result = await self._make_request(
                method="GET",
                endpoint=f"/skills/{slug}/download",
                params=params if params else None,
            )

            download_url = result.get("download_url", "")
            if not download_url:
                return DownloadResult(success=False, slug=slug, error="No download URL provided")

            path = await self._download_and_extract(download_url, target_dir)
            return DownloadResult(
                success=True,
                slug=slug,
                path=path,
                version=result.get("version", version),
            )
        except RuntimeError as e:
            return DownloadResult(success=False, slug=slug, error=str(e))

    async def _download_and_extract(
        self,
        download_url: str,
        target_dir: str | None = None,
    ) -> str:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    download_url,
                    headers=self._get_headers(),
                    timeout=60.0,
                    follow_redirects=True,
                )
                response.raise_for_status()
                zip_content = response.content
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Failed to download skill: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Download request failed: {e}") from e

        if target_dir:
            extract_path = Path(target_dir)
            extract_path.mkdir(parents=True, exist_ok=True)
        else:
            extract_path = Path(tempfile.mkdtemp(prefix="skillsmp_"))

        try:
            zip_buffer = BytesIO(zip_content)
            with zipfile.ZipFile(zip_buffer, "r") as zf:
                for member in zf.namelist():
                    member_path = Path(member)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise RuntimeError(f"Unsafe path in ZIP: {member}")
                zf.extractall(extract_path)
        except zipfile.BadZipFile as e:
            raise RuntimeError(f"Invalid ZIP file: {e}") from e

        return str(extract_path)
