"""GitHub MCP wrapper with git subprocess fallback.

Provides a thin utility layer that prefers GitHub MCP operations for remote
interactions, falling back to git subprocess when MCP is unavailable.
Local-only operations (status, diff, merge, checkout) remain as git subprocess.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import subprocess  # nosec B404 # subprocess needed for git fallback
from typing import TYPE_CHECKING, Any

from gobby.integrations.github import GitHubIntegration
from gobby.integrations.mcp_result import MCPToolResultError, parse_mcp_tool_result

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager

__all__ = [
    "GitHubMCPHelper",
    "GitHubMCPResponseError",
    "GitHubMCPToolError",
    "parse_github_repo",
]

logger = logging.getLogger(__name__)


class GitHubMCPToolError(RuntimeError):
    """The GitHub MCP server returned an in-band tool failure."""

    def __init__(self, tool_name: str, detail: str) -> None:
        self.tool_name = tool_name
        self.detail = detail
        super().__init__(f"GitHub MCP tool {tool_name!r} failed: {detail}")


class GitHubMCPResponseError(RuntimeError):
    """The GitHub MCP server returned an unexpected successful result shape."""


def parse_github_mcp_result(result: Any, tool_name: str) -> Any:
    """Parse a GitHub MCP SDK result into its domain payload."""
    try:
        return parse_mcp_tool_result(result)
    except MCPToolResultError as exc:
        raise GitHubMCPToolError(tool_name, exc.detail) from exc


def parse_github_repo(github_repo: str) -> tuple[str, str]:
    """Parse 'owner/repo' string into (owner, repo) tuple.

    Args:
        github_repo: Repository in "owner/repo" format.

    Returns:
        Tuple of (owner, repo).

    Raises:
        ValueError: If format is invalid.
    """
    if "/" not in github_repo:
        raise ValueError(f"Invalid github_repo format: {github_repo!r}, expected 'owner/repo'")
    parts = github_repo.split("/", 1)
    if not parts[0] or not parts[1]:
        raise ValueError(f"Invalid github_repo format: {github_repo!r}, expected 'owner/repo'")
    return parts[0], parts[1]


def _github_page_limit(limit: int) -> int:
    """Validate limits sent to GitHub pagination surfaces."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if limit > 100:
        raise ValueError("GitHub API pagination limit cannot exceed 100")
    return limit


def _github_result_limit(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be positive")
    return limit


def _validate_git_ref(ref: str, field_name: str) -> str:
    forbidden = (" ", "..", "~", "^", ":", "\\", "\x00", "?", "*", "[")
    if not ref or ref.startswith("-") or any(value in ref for value in forbidden):
        raise ValueError(f"{field_name} must be a valid Git ref")
    return ref


class GitHubMCPHelper:
    """GitHub MCP wrapper with git subprocess fallback.

    Provides methods for common GitHub operations that prefer the GitHub MCP
    server when available, falling back to local git commands otherwise.

    Attributes:
        github: GitHubIntegration instance for availability checks.
        mcp_manager: MCPClientManager for MCP tool calls.
        repo_path: Local repository path for git fallback.
        owner: GitHub repository owner.
        repo: GitHub repository name.
    """

    def __init__(
        self,
        mcp_manager: MCPClientManager,
        repo_path: str,
        github_repo: str,
        *,
        project_id: str | None = None,
    ) -> None:
        """Initialize GitHubMCPHelper.

        Args:
            mcp_manager: MCPClientManager for GitHub MCP server access.
            repo_path: Path to the local git repository.
            github_repo: GitHub repo in "owner/repo" format.
            project_id: Project scope used to resolve the github instance.
        """
        self.project_id = project_id
        self.github = GitHubIntegration(mcp_manager, project_id=project_id)
        self.mcp_manager = mcp_manager
        self.repo_path = repo_path
        self.owner, self.repo = parse_github_repo(github_repo)

    async def _call_github_mcp(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool on the GitHub MCP server.

        Args:
            tool_name: Name of the GitHub MCP tool.
            arguments: Tool arguments.

        Returns:
            Parsed response from the MCP tool.
        """
        from gobby.mcp_proxy.services.server_resolution import as_project_id, resolve_server

        project_id = as_project_id(
            self.project_id,
            default=as_project_id(getattr(self.mcp_manager, "project_id", None)),
        )
        config = resolve_server(self.mcp_manager, "github", project_id=project_id)
        if config is None:
            raise GitHubMCPToolError(tool_name, f"github server not found in project {project_id}")
        session = await self.mcp_manager.get_client_session(config.id)
        result = await session.call_tool(tool_name, arguments)
        return parse_github_mcp_result(result, tool_name)

    async def _paginate_github_mcp(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        limit: int,
    ) -> list[dict[str, Any]]:
        total_limit = _github_result_limit(limit)
        records: list[dict[str, Any]] = []
        page = 1

        while len(records) < total_limit:
            page_size = _github_page_limit(min(100, total_limit - len(records)))
            result = await self._call_github_mcp(
                tool_name,
                {**arguments, "page": page, "per_page": page_size},
            )
            if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
                raise GitHubMCPResponseError(
                    f"GitHub MCP tool {tool_name!r} returned an unexpected response shape."
                )
            records.extend(result)
            if len(result) < page_size:
                break
            page += 1

        return records[:total_limit]

    def _run_git(
        self,
        args: list[str],
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        """Run a git command as fallback."""
        return subprocess.run(  # nosec B603 B607
            ["git", *args],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    async def _run_git_async(
        self,
        args: list[str],
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        """Run a git command as fallback, off the event loop."""
        return await asyncio.to_thread(self._run_git, args, timeout)

    async def list_commits(
        self,
        branch: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List recent commits on a branch.

        Tries GitHub MCP first (richer data: author avatar, URL), falls back
        to git log.

        Args:
            branch: Branch name.
            limit: Maximum number of commits.

        Returns:
            List of commit dicts with sha, short_sha, message, author, date.
        """
        _validate_git_ref(branch, "branch")
        mcp_error: Exception | None = None
        if self.github.is_available():
            try:
                data = await self._paginate_github_mcp(
                    "list_commits",
                    {
                        "owner": self.owner,
                        "repo": self.repo,
                        "sha": branch,
                    },
                    limit,
                )
                commits = []
                for item in data:
                    commit_data = item.get("commit", {})
                    commits.append(
                        {
                            "sha": item.get("sha", ""),
                            "short_sha": item.get("sha", "")[:7],
                            "message": commit_data.get("message", "").split("\n")[0],
                            "author": (item.get("author") or {}).get(
                                "login",
                                commit_data.get("author", {}).get("name", ""),
                            ),
                            "date": commit_data.get("author", {}).get("date", ""),
                            "html_url": item.get("html_url", ""),
                            "author_avatar": (item.get("author") or {}).get("avatar_url", ""),
                        }
                    )
                return commits
            except Exception as exc:
                mcp_error = exc
                logger.debug("GitHub MCP list_commits failed, falling back to git", exc_info=True)

        # Fallback: git log
        try:
            return await asyncio.to_thread(self._list_commits_git, branch, limit)
        except Exception as fallback_error:
            if mcp_error is not None:
                raise fallback_error from mcp_error
            raise

    def _list_commits_git(self, branch: str, limit: int) -> list[dict[str, Any]]:
        """List commits using git log as fallback."""
        _validate_git_ref(branch, "branch")
        r = self._run_git(
            [
                "log",
                branch,
                f"--max-count={_github_result_limit(limit)}",
                "--format=%H\t%h\t%s\t%an\t%aI",
            ],
            timeout=15,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git log failed for {branch!r}: {r.stderr.strip()}")
        commits = []
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t", 4)
            if len(parts) >= 5:
                commits.append(
                    {
                        "sha": parts[0],
                        "short_sha": parts[1],
                        "message": parts[2],
                        "author": parts[3],
                        "date": parts[4],
                    }
                )
        return commits

    async def get_file_contents(
        self,
        path: str,
        branch: str | None = None,
    ) -> str:
        """Get file contents from the repository.

        Tries GitHub MCP first, falls back to git show.

        Args:
            path: File path relative to repo root.
            branch: Branch or ref (default: HEAD).

        Returns:
            File contents as string.
        """
        if branch is not None:
            _validate_git_ref(branch, "branch")
        if self.github.is_available():
            try:
                data = await self._call_github_mcp(
                    "get_file_contents",
                    {
                        "owner": self.owner,
                        "repo": self.repo,
                        "path": path,
                        **({"branch": branch} if branch else {}),
                    },
                )
                if isinstance(data, dict) and "content" in data:
                    try:
                        return base64.b64decode(data["content"]).decode("utf-8")
                    except (binascii.Error, UnicodeDecodeError):
                        logger.debug("base64 decode failed, falling back to git", exc_info=True)
                if isinstance(data, str):
                    return data
            except Exception as e:
                logger.debug("GitHub MCP get_file_contents failed, falling back: %s", e)

        # Fallback: git show
        ref = branch or "HEAD"
        try:
            r = await self._run_git_async(["show", f"{ref}:{path}"], timeout=10)
            if r.returncode == 0:
                return r.stdout
            raise FileNotFoundError(f"File not found: {path} at {ref}")
        except subprocess.TimeoutExpired as err:
            raise TimeoutError(f"git show timed out for {path}") from err

    async def create_branch(
        self,
        name: str,
        from_branch: str | None = None,
    ) -> bool:
        """Create a branch on the remote.

        Tries GitHub MCP first, falls back to git push.

        Args:
            name: New branch name.
            from_branch: Base branch (default: repo default branch).

        Returns:
            True if branch was created successfully.
        """
        _validate_git_ref(name, "name")
        if from_branch is not None:
            _validate_git_ref(from_branch, "from_branch")
        if self.github.is_available():
            try:
                args: dict[str, Any] = {
                    "owner": self.owner,
                    "repo": self.repo,
                    "branch": name,
                }
                if from_branch:
                    args["from_branch"] = from_branch

                await self._call_github_mcp("create_branch", args)
                return True
            except Exception as e:
                logger.debug("GitHub MCP create_branch failed, falling back: %s", e)

        # Fallback: git push
        try:
            base = from_branch or "HEAD"
            r = await self._run_git_async(
                ["push", "origin", f"{base}:refs/heads/{name}"], timeout=60
            )
            return r.returncode == 0
        except Exception as e:
            logger.warning("git push fallback for create_branch failed: %s", e)
            return False

    async def push_files(
        self,
        branch: str,
        files: list[dict[str, str]],
        message: str,
    ) -> dict[str, Any]:
        """Commit and push files directly to a remote branch.

        Uses the GitHub MCP push_files tool to create a commit on the remote
        without requiring a local git operation. No git fallback — this is a
        remote-only capability.

        Args:
            branch: Target branch name.
            files: List of dicts with 'path' and 'content' keys.
            message: Commit message.

        Returns:
            Dict with commit info from GitHub MCP.

        Raises:
            RuntimeError: If GitHub MCP is not available.
        """
        _validate_git_ref(branch, "branch")
        self.github.require_available()

        result = await self._call_github_mcp(
            "push_files",
            {
                "owner": self.owner,
                "repo": self.repo,
                "branch": branch,
                "files": files,
                "message": message,
            },
        )
        return result if isinstance(result, dict) else {"result": result}

    async def list_issues(
        self,
        state: str = "open",
        labels: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List repository issues via GitHub MCP.

        No git fallback — issues are a remote-only concept.

        Args:
            state: Issue state filter ("open", "closed", "all").
            labels: Optional label filter.
            limit: Maximum issues to return.

        Returns:
            List of issue dicts.

        Raises:
            RuntimeError: If GitHub MCP is not available.
        """
        self.github.require_available()

        args: dict[str, Any] = {
            "owner": self.owner,
            "repo": self.repo,
            "state": state,
        }
        if labels:
            args["labels"] = ",".join(labels)

        return await self._paginate_github_mcp("list_issues", args, limit)
