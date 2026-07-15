"""GitHub issue import operations for task synchronization."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess  # nosec B404
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


class GitHubTaskSyncMixin:
    """GitHub issue import behavior shared by the task sync facade."""

    db: HubDatabase

    async def import_from_github_issues(
        self, repo_url: str, project_id: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Import open issues from a GitHub repository as tasks."""
        from gobby.sync.tasks import (
            _ensure_task_sequence_metadata,
            _github_issue_uuid_seed,
            _legacy_github_issue_uuid_seed,
            _parse_timestamp,
        )

        try:
            match = re.match(r"https?://github\.com/([^/]+)/([^/]+)/?", repo_url)
            if not match:
                return {
                    "success": False,
                    "error": "Invalid GitHub URL. Expected: https://github.com/owner/repo",
                }
            owner, repo = match.groups()
            repo = repo.removesuffix(".git")
            github_repo = f"{owner.lower()}/{repo.lower()}"

            if not project_id:
                row = self.db.fetchone("SELECT id FROM projects WHERE github_url = %s", (repo_url,))
                if row:
                    project_id = row["id"]
            if not project_id:
                from gobby.utils.project_context import get_project_context

                ctx = get_project_context()
                if ctx and ctx.get("id"):
                    project_id = ctx["id"]
            if not project_id:
                return {
                    "success": False,
                    "error": "Could not determine project ID. Run from within a gobby project.",
                }

            issues = await self._fetch_github_issues_mcp(owner, repo, limit)
            if issues is None:
                issues = await asyncio.to_thread(
                    self._fetch_github_issues_cli, owner, repo, repo_url, limit
                )
                if issues is None:
                    return {
                        "success": False,
                        "error": "GitHub MCP unavailable and gh CLI not found. "
                        "Install from https://cli.github.com/",
                    }
            if not issues:
                return {
                    "success": True,
                    "message": "No open issues found",
                    "imported": [],
                    "count": 0,
                }

            imported = []
            imported_count = 0
            with self.db.transaction() as conn:
                for issue in issues:
                    issue_num = issue.get("number")
                    if type(issue_num) is not int:
                        continue
                    task_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            _github_issue_uuid_seed(project_id, owner, repo, issue_num),
                        )
                    )
                    legacy_normalized_task_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            _legacy_github_issue_uuid_seed(owner, repo, issue_num),
                        )
                    )
                    legacy_task_id = str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"{repo_url}/issues/{issue_num}")
                    )
                    title = issue.get("title", "Untitled Issue")
                    body = issue.get("body") or ""
                    desc = f"{body}\n\nSource: {repo_url}/issues/{issue_num}".strip()
                    labels = [lbl.get("name") for lbl in issue.get("labels", []) if lbl.get("name")]
                    labels_json = json.dumps(labels) if labels else None
                    created_at = _parse_timestamp(
                        issue.get("createdAt") or issue.get("created_at") or datetime.now(UTC)
                    )
                    updated_at = datetime.now(UTC)

                    existing = conn.execute(
                        """
                        SELECT id
                          FROM tasks
                         WHERE project_id = %s
                           AND github_repo = %s
                           AND github_issue_number = %s
                         LIMIT 1
                        """,
                        (project_id, github_repo, issue_num),
                    ).fetchone()
                    if existing is None:
                        for candidate_task_id in (
                            task_id,
                            legacy_normalized_task_id,
                            legacy_task_id,
                        ):
                            existing = conn.execute(
                                "SELECT id FROM tasks WHERE project_id = %s AND id = %s",
                                (project_id, candidate_task_id),
                            ).fetchone()
                            if existing is not None:
                                break
                    if existing:
                        task_id = str(existing["id"])
                        conn.execute(
                            """
                            UPDATE tasks
                               SET title=%s,
                                   description=%s,
                                   labels=%s,
                                   updated_at=%s,
                                   github_repo=%s,
                                   github_issue_number=%s
                             WHERE project_id=%s
                               AND id=%s
                            """,
                            (
                                title,
                                desc,
                                labels_json,
                                updated_at,
                                github_repo,
                                issue_num,
                                project_id,
                                task_id,
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO tasks (
                                id, project_id, title, description, task_type,
                                labels, created_at, updated_at,
                                github_repo, github_issue_number
                            ) VALUES (%s, %s, %s, %s, 'task', %s, %s, %s, %s, %s)
                            """,
                            (
                                task_id,
                                project_id,
                                title,
                                desc,
                                labels_json,
                                created_at,
                                updated_at,
                                github_repo,
                                issue_num,
                            ),
                        )
                        imported_count += 1
                    _ensure_task_sequence_metadata(
                        conn,
                        project_id=project_id,
                        task_id=task_id,
                        updated_at=updated_at,
                    )
                    imported.append(task_id)

            return {
                "success": True,
                "imported": imported,
                "count": imported_count,
                "message": f"Imported {imported_count} new issues, updated {len(imported) - imported_count} existing.",
            }
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse GitHub response: {e}")
            return {"success": False, "error": f"Failed to parse GitHub response: {e}"}
        except Exception as e:
            logger.error(f"Failed to import from GitHub: {e}")
            return {"success": False, "error": str(e)}

    async def _fetch_github_issues_mcp(
        self, owner: str, repo: str, limit: int
    ) -> list[dict[str, Any]] | None:
        """Fetch issues using GitHub MCP server. Returns None if unavailable."""
        try:
            from gobby.app_context import get_app_context
            from gobby.integrations.github import GitHubIntegration

            ctx = get_app_context()
            if not ctx or not ctx.mcp_manager:
                return None
            gh = GitHubIntegration(ctx.mcp_manager)
            if not gh.is_available():
                return None
            session = await ctx.mcp_manager.get_client_session("github")
            result = await session.call_tool(
                "list_issues",
                {
                    "owner": owner,
                    "repo": repo,
                    "state": "open",
                    "per_page": min(limit, 100),
                },
            )
            if hasattr(result, "content") and result.content:
                for item in result.content:
                    if hasattr(item, "text"):
                        try:
                            data = json.loads(item.text)
                            return data if isinstance(data, list) else []
                        except (json.JSONDecodeError, TypeError):
                            pass
            return []
        except Exception as e:
            logger.debug(f"GitHub MCP issue fetch failed: {e}")
            return None

    def _fetch_github_issues_cli(
        self, owner: str, repo: str, repo_url: str, limit: int
    ) -> list[dict[str, Any]] | None:
        """Fetch issues using gh CLI. Returns None if gh is not available."""
        try:
            subprocess.run(["gh", "--version"], capture_output=True, check=True)  # nosec B603 B607
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
        cmd = [
            "gh",
            "issue",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,body,labels,createdAt",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603
        if result.returncode != 0:
            raise RuntimeError(f"gh command failed: {result.stderr}")
        parsed: list[dict[str, Any]] = json.loads(result.stdout)
        return parsed
