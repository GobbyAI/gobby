"""MCP server import functionality."""

import asyncio
import logging
import os
import random
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from gobby.config.app import DaemonConfig
from gobby.prompts import PromptLoader
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.secret_names import SECRET_REF_PATTERN
from gobby.utils.http_retry import parse_retry_after
from gobby.utils.json_helpers import extract_json_object

if TYPE_CHECKING:
    from gobby.llm.service import LLMService
    from gobby.mcp_proxy.manager import MCPClientManager

logger = logging.getLogger(__name__)

# Pattern to detect placeholder secrets like <YOUR_API_KEY>
SECRET_PLACEHOLDER_PATTERN = re.compile(r"<YOUR_[A-Z0-9_]+>")
GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "gobby-mcp-importer",
}
GITHUB_RAW_HEADERS = {
    "Accept": "application/vnd.github.raw",
    "User-Agent": "gobby-mcp-importer",
}
MAX_IMPORT_CONTEXT_CHARS = 14_000
MAX_SEARCH_REPOSITORIES = 3
GITHUB_REQUEST_ATTEMPTS = 3
GITHUB_BASE_BACKOFF_SECONDS = 0.4
GITHUB_MAX_RETRY_AFTER_SECONDS = 30.0
GITHUB_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


class MCPServerImporter:
    """Handles importing MCP servers from various sources."""

    def __init__(
        self,
        config: DaemonConfig,
        db: HubDatabase,
        current_project_id: str,
        mcp_client_manager: "MCPClientManager | None" = None,
        llm_service: "LLMService | None" = None,
    ):
        """
        Initialize the importer.

        Args:
            config: Daemon configuration
            db: Database connection
            current_project_id: ID of the current project to import into
            mcp_client_manager: Optional MCP client manager for live connections
            llm_service: LLM service for SDK calls (routes through provider for hook suppression)
        """
        self.config = config
        self.db = db
        self.current_project_id = current_project_id
        self.mcp_db_manager = LocalMCPManager(db)
        self.project_manager = LocalProjectManager(db)
        self.mcp_client_manager = mcp_client_manager
        self.llm_service = llm_service
        self.import_config = config.get_import_mcp_server_config()

        # Initialize prompt loader
        self._loader = PromptLoader(db=self.db)

    async def import_from_project(
        self,
        source_project: str,
        servers: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Import MCP servers from another Gobby project.

        Args:
            source_project: Source project name or ID
            servers: Optional list of server names to import (imports all if None)

        Returns:
            Result dict with imported servers or error
        """
        # Resolve source project - try by name first, then by ID
        project = self.project_manager.get_by_name(source_project)
        if not project:
            project = self.project_manager.get(source_project)

        if not project:
            # List available projects for helpful error message
            available = self.project_manager.list()
            project_names = [p.name for p in available]
            return {
                "success": False,
                "error": f"Project '{source_project}' not found",
                "available_projects": project_names,
            }

        # Get servers from source project
        source_servers = self.mcp_db_manager.list_servers(
            project_id=project.id,
            enabled_only=False,  # Include disabled servers too
        )

        if not source_servers:
            return {
                "success": False,
                "error": f"No MCP servers found in project '{project.name}'",
            }

        # Filter by server names if specified
        if servers:
            servers_lower = [s.lower() for s in servers]
            source_servers = [s for s in source_servers if s.name.lower() in servers_lower]
            if not source_servers:
                return {
                    "success": False,
                    "error": f"None of the specified servers found in project '{project.name}'",
                    "requested": servers,
                }

        # Get existing servers in current project to skip duplicates
        existing_servers = self.mcp_db_manager.list_servers(
            project_id=self.current_project_id,
            enabled_only=False,
        )
        existing_names = {s.name.lower() for s in existing_servers}

        # Import each server
        imported = []
        skipped = []
        failed = []

        for server in source_servers:
            if server.name.lower() in existing_names:
                skipped.append(server.name)
                continue

            # Add server using action (connects and saves) or just save to db
            add_result = await self._add_server(
                name=server.name,
                transport=server.transport,
                url=server.url,
                command=server.command,
                args=server.args,
                env=server.env,
                headers=server.headers,
                enabled=server.enabled,
                description=server.description,
            )

            if add_result.get("success"):
                imported.append(server.name)
            else:
                failed.append({"name": server.name, "error": add_result.get("error")})

        result: dict[str, Any] = {
            "success": len(imported) > 0 or len(failed) == 0,
            "imported": imported,
            "message": f"Imported {len(imported)} server(s) from project '{project.name}'",
        }

        if skipped:
            result["skipped"] = skipped
            result["message"] += f" (skipped {len(skipped)} existing)"

        if failed:
            result["failed"] = failed

        return result

    async def import_from_github(self, github_url: str) -> dict[str, Any]:
        """
        Import MCP server from GitHub repository.

        Fetches repository context through GitHub APIs, then uses the configured
        import_mcp_server feature to synthesize a server config.

        Args:
            github_url: GitHub repository URL

        Returns:
            Result dict with config (may need user input for secrets)
        """
        if not self.import_config.enabled:
            return {
                "success": False,
                "error": "MCP server import is disabled in configuration",
            }

        try:
            if not self.llm_service:
                raise RuntimeError("LLM service not initialized")

            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                repository_context = await self._fetch_github_repository_context(
                    client,
                    github_url,
                )

            # Build prompt to fetch and extract config
            prompt_path = self.import_config.github_fetch_prompt_path or "import/github_fetch"
            prompt = self._render_import_prompt(
                prompt_path,
                {"github_url": github_url},
                repository_context,
            )

            # Get system prompt
            sys_prompt_path = self.import_config.prompt_path or "import/system"
            system_prompt = self._loader.render(sys_prompt_path, {})

            result_text = await self.llm_service.call_feature(
                self.import_config,
                prompt=prompt,
                system_prompt=system_prompt,
                caller="mcp_proxy.importer.github",
            )

            # Parse synthesized config into a preview; explicit add-server is
            # the approval/install step.
            return await self._parse_and_add_config(result_text)

        except Exception as e:
            logger.error("Failed to import from GitHub: %s", e)
            return {
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__,
            }

    async def import_from_query(self, search_query: str) -> dict[str, Any]:
        """
        Import MCP server by searching for it.

        Searches GitHub deterministically, fetches candidate repository context,
        then uses the configured import_mcp_server feature to synthesize a config.

        Args:
            search_query: Natural language search query

        Returns:
            Result dict with config (may need user input for secrets)
        """
        if not self.import_config.enabled:
            return {
                "success": False,
                "error": "MCP server import is disabled in configuration",
            }

        try:
            if not self.llm_service:
                raise RuntimeError("LLM service not initialized")

            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                search_context = await self._fetch_github_search_context(
                    client,
                    search_query,
                )

            # Build prompt to search and extract config
            prompt_path = self.import_config.search_fetch_prompt_path or "import/search_fetch"
            prompt = self._render_import_prompt(
                prompt_path,
                {"search_query": search_query},
                search_context,
            )

            # Get system prompt
            sys_prompt_path = self.import_config.prompt_path or "import/system"
            system_prompt = self._loader.render(sys_prompt_path, {})

            result_text = await self.llm_service.call_feature(
                self.import_config,
                prompt=prompt,
                system_prompt=system_prompt,
                caller="mcp_proxy.importer.query",
            )

            # Parse synthesized config into a preview; explicit add-server is
            # the approval/install step.
            return await self._parse_and_add_config(result_text)

        except Exception as e:
            logger.error("Failed to import from query: %s", e)
            return {
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__,
            }

    def _render_import_prompt(
        self,
        prompt_path: str,
        context: dict[str, Any],
        fetched_context: str,
    ) -> str:
        render_context = {
            **context,
            "fetched_context": fetched_context,
        }
        base_prompt = self._loader.render(prompt_path, render_context)
        return (
            f"{base_prompt}\n\n"
            "Fetched documentation context:\n"
            f"{self._truncate_context(fetched_context, context.get('github_url') or context['search_query'])}\n\n"
            "Use only the fetched documentation context above. "
            "Return the JSON object requested by the system prompt."
        )

    def _github_repo_from_url(self, github_url: str) -> tuple[str, str]:
        parsed = urlparse(github_url)
        if not parsed.netloc and parsed.path.startswith("github.com/"):
            parsed = urlparse(f"https://{github_url}")

        hostname = parsed.netloc.casefold().removeprefix("www.")
        if hostname != "github.com":
            raise ValueError("GitHub import URL must use github.com")

        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise ValueError("GitHub import URL must include owner and repository")

        owner = parts[0].strip()
        repo = parts[1].strip().removesuffix(".git")
        if not owner or not repo:
            raise ValueError("GitHub import URL must include owner and repository")

        return owner, repo

    async def _fetch_github_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._fetch_github_response(
            client,
            f"{GITHUB_API_BASE}{path}",
            base_headers=GITHUB_API_HEADERS,
            params=params,
        )
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"GitHub API returned non-object response for {path}")
        return data

    async def _fetch_github_readme(
        self,
        client: httpx.AsyncClient,
        owner: str,
        repo: str,
    ) -> str:
        response = await self._fetch_github_response(
            client,
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/readme",
            base_headers=GITHUB_RAW_HEADERS,
        )
        return response.text

    async def _fetch_github_response(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        base_headers: dict[str, str],
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = self._github_headers(base_headers)
        for attempt in range(GITHUB_REQUEST_ATTEMPTS):
            try:
                response = await client.get(url, headers=headers, params=params)
                if attempt < GITHUB_REQUEST_ATTEMPTS - 1 and self._should_retry_github_response(
                    response
                ):
                    await asyncio.sleep(self._github_retry_delay(response, attempt))
                    continue
                self._raise_for_github_status(response)
                return response
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt >= GITHUB_REQUEST_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(self._github_retry_delay(None, attempt))
        raise RuntimeError(f"GitHub request failed after retries: {url}")

    def _github_headers(self, base_headers: dict[str, str]) -> dict[str, str]:
        headers = dict(base_headers)
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _should_retry_github_response(self, response: Any) -> bool:
        status_code = getattr(response, "status_code", None)
        if status_code in GITHUB_RETRY_STATUS_CODES:
            return True
        return status_code == 403 and self._github_rate_limited(response)

    def _github_retry_delay(self, response: Any | None, attempt: int) -> float:
        retry_after = (
            parse_retry_after(
                getattr(response, "headers", {}).get("Retry-After"),
                max_delay=GITHUB_MAX_RETRY_AFTER_SECONDS,
            )
            if response is not None
            else None
        )
        base_delay = retry_after
        if base_delay is None:
            base_delay = GITHUB_BASE_BACKOFF_SECONDS * (2**attempt)
        # Retry jitter does not require cryptographic randomness.
        jitter = random.uniform(0.0, min(0.25, base_delay * 0.2))  # nosec B311
        return base_delay + jitter

    def _github_rate_limited(self, response: Any) -> bool:
        if getattr(response, "status_code", None) == 429:
            return True
        headers = getattr(response, "headers", {})
        if headers.get("x-ratelimit-remaining") == "0":
            return True
        if headers.get("Retry-After"):
            return True
        try:
            data = response.json()
        except ValueError:
            return False
        if not isinstance(data, dict):
            return False
        message = str(data.get("message") or "").lower()
        return "rate limit" in message or "secondary rate limit" in message

    def _raise_for_github_status(self, response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if response.status_code in {403, 429} and self._github_rate_limited(response):
                raise RuntimeError(self._github_rate_limit_message(response)) from exc
            raise

    def _github_rate_limit_message(self, response: Any) -> str:
        message = "GitHub API rate limit exceeded"
        retry_after = parse_retry_after(
            getattr(response, "headers", {}).get("Retry-After"),
            max_delay=GITHUB_MAX_RETRY_AFTER_SECONDS,
        )
        if retry_after is not None:
            message += f"; retry after {retry_after:.0f}s"
        if not os.environ.get("GITHUB_TOKEN", "").strip():
            message += "; set GITHUB_TOKEN to use an authenticated GitHub API limit"
        return message

    async def _fetch_github_repository_context(
        self,
        client: httpx.AsyncClient,
        github_url: str,
    ) -> str:
        owner, repo = self._github_repo_from_url(github_url)
        metadata = await self._fetch_github_json(client, f"/repos/{owner}/{repo}")
        try:
            readme = await self._fetch_github_readme(client, owner, repo)
        except httpx.HTTPError as exc:
            readme = f"README unavailable: {exc}"

        return "\n".join(
            [
                f"GitHub repository: {owner}/{repo}",
                f"URL: https://github.com/{owner}/{repo}",
                f"Description: {metadata.get('description') or ''}",
                f"Default branch: {metadata.get('default_branch') or ''}",
                f"Primary language: {metadata.get('language') or ''}",
                "",
                "README:",
                readme,
            ]
        )

    async def _fetch_github_search_context(
        self,
        client: httpx.AsyncClient,
        search_query: str,
    ) -> str:
        search = await self._fetch_github_json(
            client,
            "/search/repositories",
            params={
                "q": self._github_repository_search_query(search_query),
                "sort": "stars",
                "order": "desc",
                "per_page": MAX_SEARCH_REPOSITORIES,
            },
        )
        raw_items = search.get("items")
        items = raw_items if isinstance(raw_items, list) else []
        if not items:
            return f"No GitHub repositories found for query: {search_query}"

        sections = [f"GitHub repository search query: {search_query}"]
        for index, item in enumerate(items[:MAX_SEARCH_REPOSITORIES], start=1):
            if not isinstance(item, dict):
                continue
            full_name = str(item.get("full_name") or "")
            owner, separator, repo = full_name.partition("/")
            if not separator or not owner or not repo:
                continue

            try:
                readme = await self._fetch_github_readme(client, owner, repo)
            except httpx.HTTPError as exc:
                readme = f"README unavailable: {exc}"

            sections.extend(
                [
                    "",
                    f"Candidate {index}: {full_name}",
                    f"URL: {item.get('html_url') or f'https://github.com/{full_name}'}",
                    f"Description: {item.get('description') or ''}",
                    f"Primary language: {item.get('language') or ''}",
                    f"Stars: {item.get('stargazers_count') or 0}",
                    "README:",
                    readme,
                ]
            )

        return "\n".join(sections)

    def _github_repository_search_query(self, search_query: str) -> str:
        normalized = search_query.casefold()
        terms = [search_query]
        if "mcp" not in normalized:
            terms.append("mcp")
        if "server" not in normalized:
            terms.append("server")
        return " ".join(term for term in terms if term.strip())

    def _truncate_context(self, text: str, source: str) -> str:
        if len(text) <= MAX_IMPORT_CONTEXT_CHARS:
            return text
        truncated = text[:MAX_IMPORT_CONTEXT_CHARS].rstrip()
        return (
            f"{truncated}\n\n"
            f"[docs truncated: first {len(truncated)} of {len(text)} chars; source: {source}]"
        )

    async def _add_server(
        self,
        name: str,
        transport: str,
        url: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        enabled: bool = True,
        description: str | None = None,
    ) -> dict[str, Any]:
        """
        Add an MCP server using the action (connects + saves) or db-only fallback.

        Args:
            name: Server name
            transport: Transport type
            url: Server URL (for http/websocket)
            command: Command (for stdio)
            args: Command args (for stdio)
            env: Environment variables
            headers: HTTP headers
            enabled: Whether server is enabled
            description: Server description

        Returns:
            Result dict with success status
        """
        try:
            if self.mcp_client_manager:
                # Use the action which connects and saves
                from gobby.mcp_proxy.actions import add_mcp_server

                result: dict[str, Any] = await add_mcp_server(
                    mcp_manager=self.mcp_client_manager,
                    name=name,
                    transport=transport,
                    project_id=self.current_project_id,
                    url=url,
                    headers=headers,
                    command=command,
                    args=args,
                    env=env,
                    enabled=enabled,
                    description=description,
                )
                return result
            else:
                # Fallback to db-only (won't be connected until restart)
                self.mcp_db_manager.upsert(
                    name=name,
                    transport=transport,
                    project_id=self.current_project_id,
                    url=url,
                    command=command,
                    args=args,
                    env=env,
                    headers=headers,
                    enabled=enabled,
                    description=description,
                )
                return {
                    "success": True,
                    "imported": [name],
                    "message": f"Successfully added MCP server '{name}' (restart daemon to connect)",
                }

        except Exception as e:
            logger.error("Failed to add server '%s': %s", name, e)
            return {
                "success": False,
                "name": name,
                "error": str(e),
                "error_type": type(e).__name__,
            }

    async def _parse_and_add_config(self, result_text: str) -> dict[str, Any]:
        """
        Parse LLM response and return a non-mutating preview.

        Args:
            result_text: Raw text from LLM

        Returns:
            Preview result requiring approval, or needs_configuration if secrets are required
        """
        # Try to extract JSON from the response
        config = self._extract_json(result_text)

        if not config:
            return {
                "success": False,
                "error": "Could not extract valid configuration from documentation",
                "raw_response": result_text[:1000],  # Include first 1000 chars for debugging
            }

        # Check for missing secrets
        missing = self._find_missing_secrets(config)
        instructions = config.pop("instructions", None)

        name = config.get("name")
        transport = config.get("transport")

        if not name or not transport:
            return {
                "success": False,
                "error": "Extracted config missing required fields: name or transport",
                "config": config,
            }

        if missing:
            # Secrets needed - return config for user to fill in.
            result: dict[str, Any] = {
                "success": True,
                "status": "needs_configuration",
                "requires_approval": True,
                "config": config,
                "missing": missing,
            }
            if instructions:
                result["instructions"] = instructions
            return result

        result = {
            "success": True,
            "status": "requires_approval",
            "requires_approval": True,
            "config": config,
            "missing": [],
        }
        if instructions:
            result["instructions"] = instructions
        return result

    def _extract_json(self, text: str) -> dict[str, Any] | None:
        """
        Extract JSON object from text.

        Handles JSON in code blocks or raw JSON.

        Args:
            text: Text potentially containing JSON

        Returns:
            Parsed JSON dict or None
        """
        result = extract_json_object(text)
        if result is None:
            return None

        # Validate it looks like a server config
        if "name" in result or "transport" in result:
            return result

        return None

    def _find_missing_secrets(self, config: dict[str, Any]) -> list[str]:
        """
        Find placeholder secrets in config.

        Args:
            config: Server configuration dict

        Returns:
            List of placeholder secret names
        """
        missing: list[str] = []
        secret_names: list[str] = []

        def check_value(value: Any, path: str = "") -> None:
            if isinstance(value, str):
                match = SECRET_PLACEHOLDER_PATTERN.search(value)
                if match:
                    missing.append(match.group(0))
                for ref in SECRET_REF_PATTERN.finditer(value):
                    secret_names.append(ref.group(1).lower())
            elif isinstance(value, dict):
                for k, v in value.items():
                    check_value(v, f"{path}.{k}" if path else k)
            elif isinstance(value, list):
                for i, v in enumerate(value):
                    check_value(v, f"{path}[{i}]")

        check_value(config)
        if secret_names:
            from gobby.storage.secrets import SecretStore

            store = SecretStore(self.db)
            for name in dict.fromkeys(secret_names):
                if not store.exists(name, project_id=self.current_project_id):
                    missing.append(f"$secret:{name}")
        return missing
