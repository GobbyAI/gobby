"""ClawHub provider implementation.

This module provides the ClawdHubProvider class which wraps the official
`clawhub` CLI tool to provide skill search, listing, and download functionality.

CLI docs: https://github.com/openclaw/clawhub/blob/main/docs/cli.md
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from gobby.skills.hubs.base import DownloadResult, HubProvider, HubSkillDetails, HubSkillInfo

logger = logging.getLogger(__name__)


class ClawdHubProvider(HubProvider):
    """Provider for ClawHub skill registry using the CLI tool.

    This provider wraps the official `clawhub` CLI (installed via
    `npm i -g clawhub`) to provide access to the ClawHub skill registry.

    The CLI provides commands for:
    - search: Vector search for skills (text output only)
    - explore: Browse latest skills (supports --json)
    - inspect: Get detailed skill metadata (supports --json)
    - install: Download/install a skill
    - list: List locally installed skills
    """

    def __init__(
        self,
        hub_name: str,
        base_url: str,
        auth_token: str | None = None,
    ) -> None:
        super().__init__(hub_name=hub_name, base_url=base_url, auth_token=auth_token)
        self._cli_available: bool | None = None
        self._cli_binary: str | None = None

    @property
    def provider_type(self) -> str:
        return "clawdhub"

    async def _check_cli_available(self) -> bool:
        """Check if the clawhub CLI is available.

        Uses `--cli-version` flag per the clawhub CLI interface.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                "clawhub",
                "--cli-version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                version = stdout.decode().strip() or stderr.decode().strip()
                logger.debug(f"ClawHub CLI version: {version}")
                self._cli_binary = "clawhub"
                return True
            return False
        except FileNotFoundError:
            logger.warning("ClawHub CLI not found. Install with: npm i -g clawhub")
            return False
        except Exception as e:
            logger.error(f"Error checking ClawHub CLI: {e}")
            return False

    async def _run_cli_command(
        self,
        command: str,
        args: list[str] | None = None,
        json_output: bool = False,
    ) -> str:
        """Run a CLI command and return raw output.

        Args:
            command: The CLI command (search, explore, inspect, install, etc.)
            args: Additional arguments for the command
            json_output: Whether to add --json flag (only explore/inspect support it)

        Returns:
            Raw stdout output from the CLI

        Raises:
            RuntimeError: If CLI is not available or command fails
        """
        if not self._cli_binary:
            raise RuntimeError("ClawHub CLI not found. Install with: npm i -g clawhub")

        cmd_args = [self._cli_binary, command]
        if args:
            cmd_args.extend(args)
        if json_output:
            cmd_args.append("--json")

        # Add auth token if available
        if self.auth_token:
            cmd_args.extend(["--token", self.auth_token])

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                logger.error(f"ClawHub CLI error: {error_msg}")
                raise RuntimeError(f"ClawHub CLI command failed: {error_msg}")

            return stdout.decode().strip()
        except FileNotFoundError as e:
            raise RuntimeError("ClawHub CLI not found. Install with: npm i -g clawhub") from e

    async def _run_cli_json(
        self,
        command: str,
        args: list[str] | None = None,
    ) -> dict[str, Any] | list[Any]:
        """Run a CLI command that supports --json and return parsed output.

        Args:
            command: The CLI command (explore, inspect)
            args: Additional arguments

        Returns:
            Parsed JSON output

        Raises:
            RuntimeError: If CLI fails or output isn't valid JSON
        """
        output = await self._run_cli_command(command, args, json_output=True)
        if not output:
            return {}

        try:
            parsed: dict[str, Any] | list[Any] = json.loads(output)
            return parsed
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse CLI JSON output: {e}")
            raise RuntimeError(f"Invalid JSON from ClawHub CLI: {e}") from e

    @staticmethod
    def _parse_search_text(output: str) -> list[dict[str, str]]:
        """Parse text output from `clawhub search`.

        Search output format is typically:
            slug  vX.Y.Z  summary text here

        Args:
            output: Raw text output from search command

        Returns:
            List of dicts with slug, version, description
        """
        results: list[dict[str, str]] = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            # Skip spinner/status lines (contain ANSI codes or common prefixes)
            if line.startswith(("-", "✖", "✔", "\x1b")):
                continue
            # Try to parse "slug  vX.Y.Z  description" format
            match = re.match(r"^(\S+)\s+v(\S+)\s+(.*)", line)
            if match:
                results.append(
                    {
                        "slug": match.group(1),
                        "version": match.group(2),
                        "description": match.group(3).strip(),
                    }
                )
            else:
                # Fallback: treat whole line as slug
                parts = line.split(None, 1)
                if parts:
                    results.append(
                        {
                            "slug": parts[0],
                            "version": "",
                            "description": parts[1] if len(parts) > 1 else "",
                        }
                    )
        return results

    async def discover(self) -> dict[str, Any]:
        """Discover hub capabilities and check CLI availability."""
        cli_available = await self._check_cli_available()
        self._cli_available = cli_available

        return {
            "hub_name": self.hub_name,
            "provider_type": self.provider_type,
            "cli_available": cli_available,
            "cli_binary": self._cli_binary,
            "base_url": self.base_url,
        }

    async def search(
        self,
        query: str,
        limit: int = 20,
    ) -> list[HubSkillInfo]:
        """Search for skills matching a query.

        Note: The search command does not support --json output,
        so we parse the text output.
        """
        if self._cli_available is None:
            self._cli_available = await self._check_cli_available()
        if not self._cli_available:
            raise RuntimeError("ClawHub CLI not installed. Install with: npm i -g clawhub")

        args = [query, "--limit", str(limit)]
        output = await self._run_cli_command("search", args)
        skills = self._parse_search_text(output)

        return [
            HubSkillInfo(
                slug=skill.get("slug", ""),
                display_name=skill.get("slug", ""),
                description=skill.get("description", ""),
                hub_name=self.hub_name,
                version=skill.get("version") or None,
            )
            for skill in skills
        ]

    async def list_skills(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[HubSkillInfo]:
        """Browse available skills from the registry.

        Uses `explore --json` for remote listing (not `list` which is local-only).
        """
        if self._cli_available is None:
            self._cli_available = await self._check_cli_available()
        if not self._cli_available:
            raise RuntimeError("ClawHub CLI not installed. Install with: npm i -g clawhub")

        args = ["--limit", str(limit)]
        result = await self._run_cli_json("explore", args)

        # Handle both list and dict responses
        skills: list[Any] = []
        if isinstance(result, list):
            skills = result
        elif isinstance(result, dict):
            skills = result.get("skills", [])

        return [
            HubSkillInfo(
                slug=skill.get("slug", skill.get("name", "")),
                display_name=skill.get("name", skill.get("slug", "")),
                description=skill.get("description", skill.get("summary", "")),
                hub_name=self.hub_name,
                version=skill.get("version"),
            )
            for skill in skills
        ]

    async def get_skill_details(
        self,
        slug: str,
    ) -> HubSkillDetails | None:
        """Get detailed information about a specific skill.

        Uses `inspect --json` which returns skill metadata and version info.
        """
        try:
            result = await self._run_cli_json("inspect", [slug])

            if not result or not isinstance(result, dict):
                return None

            return HubSkillDetails(
                slug=result.get("slug", slug),
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
        """Download and install a skill from the hub.

        Uses `clawhub install <slug>` which handles download, extraction,
        and lockfile updates.
        """
        if self._cli_available is None:
            self._cli_available = await self._check_cli_available()
        if not self._cli_available:
            return DownloadResult(
                success=False,
                slug=slug,
                error="ClawHub CLI not installed. Install with: npm i -g clawhub",
            )

        args = [slug]

        if version:
            args.extend(["--version", version])

        if target_dir:
            args.extend(["--dir", target_dir])

        # Use --force to overwrite existing without prompts
        args.append("--force")

        try:
            await self._run_cli_command("install", args)
            return DownloadResult(
                success=True,
                slug=slug,
                version=version,
            )
        except RuntimeError as e:
            return DownloadResult(
                success=False,
                slug=slug,
                error=str(e),
            )
