"""Import GitHub issues into task storage."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess  # nosec B404
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.storage.tasks import LocalTaskManager
from gobby.sync.github_validation import normalize_github_issue_number
from gobby.sync.tasks import _parse_timestamp
from gobby.tasks.import_criteria import external_issue_validation_criteria

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

GITHUB_CLI_TIMEOUT_SECONDS = 180


def _normalize_labels(raw_labels: object) -> list[str]:
    if not isinstance(raw_labels, list):
        return []

    labels: list[str] = []
    for raw_label in raw_labels:
        if isinstance(raw_label, str):
            name = raw_label
        elif isinstance(raw_label, Mapping):
            raw_name = raw_label.get("name")
            if not isinstance(raw_name, str):
                continue
            name = raw_name
        else:
            continue

        normalized_name = name.strip()
        if normalized_name:
            labels.append(normalized_name)
    return labels


def _normalize_created_at(
    raw_created_at: object,
    *,
    issue_num: int,
    github_repo: str,
) -> datetime:
    if raw_created_at is None or raw_created_at == "":
        return datetime.now(UTC)

    try:
        if isinstance(raw_created_at, str):
            return _parse_timestamp(raw_created_at.strip())
        if isinstance(raw_created_at, datetime):
            return _parse_timestamp(raw_created_at)
    except ValueError:
        pass

    logger.warning(
        "Using current time for GitHub issue with invalid creation timestamp",
        extra={
            "github_issue_number": issue_num,
            "github_repo": github_repo,
        },
    )
    return datetime.now(UTC)


class GitHubIssueImporter:
    """Import GitHub issues into task storage."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db
        self.task_manager = LocalTaskManager(db)

    async def import_from_github_issues(
        self, repo_url: str, project_id: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Import open issues from a GitHub repository as tasks."""
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

            issues = await self._fetch_github_issues_mcp(owner, repo, limit, project_id=project_id)
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

            imported, imported_count = await asyncio.to_thread(
                self._upsert_issues,
                issues,
                project_id,
                owner,
                repo,
                repo_url,
                github_repo,
            )

            return {
                "success": True,
                "imported": imported,
                "count": imported_count,
                "message": f"Imported {imported_count} new issues, updated {len(imported) - imported_count} existing.",
            }
        except json.JSONDecodeError as e:
            logger.error("Failed to parse GitHub response: %s", e)
            return {"success": False, "error": f"Failed to parse GitHub response: {e}"}
        except Exception as e:
            logger.error("Failed to import from GitHub: %s", e)
            return {"success": False, "error": str(e)}

    def _upsert_issues(
        self,
        issues: list[dict[str, Any]],
        project_id: str,
        owner: str,
        repo: str,
        repo_url: str,
        github_repo: str,
    ) -> tuple[list[str], int]:
        from gobby.sync.tasks import (
            _ensure_task_sequence_metadata,
            _github_issue_uuid_seed,
        )

        imported: list[str] = []
        imported_count = 0
        with self.db.transaction() as conn:
            for issue in issues:
                raw_issue_num = issue.get("number")
                issue_num = normalize_github_issue_number(raw_issue_num)
                if type(raw_issue_num) is not int or issue_num is None:
                    logger.warning(
                        "Skipping GitHub issue with invalid number",
                        extra={
                            "github_issue_number": raw_issue_num,
                            "github_repo": github_repo,
                        },
                    )
                    continue
                task_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        _github_issue_uuid_seed(project_id, owner, repo, issue_num),
                    )
                )
                title = issue.get("title", "Untitled Issue")
                body = issue.get("body") or ""
                desc = f"{body}\n\nSource: {repo_url}/issues/{issue_num}".strip()
                labels = _normalize_labels(issue.get("labels"))
                labels_json = json.dumps(labels) if labels else None
                created_at = _normalize_created_at(
                    issue.get("createdAt") or issue.get("created_at"),
                    issue_num=issue_num,
                    github_repo=github_repo,
                )
                updated_at = datetime.now(UTC)
                validation_criteria = external_issue_validation_criteria(
                    "GitHub",
                    f"{github_repo}#{issue_num}",
                )

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
                    existing = conn.execute(
                        "SELECT id FROM tasks WHERE project_id = %s AND id = %s",
                        (project_id, task_id),
                    ).fetchone()
                if existing:
                    task_id = str(existing["id"])
                    conn.execute(
                        """
                        UPDATE tasks
                           SET title=%s,
                               description=%s,
                               labels=%s,
                               validation_criteria=%s,
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
                            validation_criteria,
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
                            labels, validation_criteria, created_at, updated_at,
                            github_repo, github_issue_number
                        ) VALUES (%s, %s, %s, %s, 'task', %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            task_id,
                            project_id,
                            title,
                            desc,
                            labels_json,
                            validation_criteria,
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
        return imported, imported_count

    async def _fetch_github_issues_mcp(
        self, owner: str, repo: str, limit: int, *, project_id: str
    ) -> list[dict[str, Any]] | None:
        """Fetch issues using GitHub MCP server. Returns None if unavailable."""
        try:
            from gobby.app_context import get_app_context
            from gobby.integrations.github import GitHubIntegration

            ctx = get_app_context()
            if not ctx or not ctx.mcp_manager:
                return None
            from gobby.mcp_proxy.services.server_resolution import resolved_server_id

            gh = GitHubIntegration(ctx.mcp_manager, project_id=project_id)
            if not gh.is_available():
                return None
            server_id = resolved_server_id(ctx.mcp_manager, "github", project_id=project_id)
            if server_id is None:
                return None
            session = await ctx.mcp_manager.get_client_session(server_id)
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
            logger.debug("GitHub MCP issue fetch failed: %s", e)
            return None

    def _fetch_github_issues_cli(
        self, owner: str, repo: str, repo_url: str, limit: int
    ) -> list[dict[str, Any]] | None:
        """Fetch issues using gh CLI. Returns None if gh is not available."""
        try:
            subprocess.run(  # nosec B603 B607
                ["gh", "--version"],
                capture_output=True,
                check=True,
                timeout=GITHUB_CLI_TIMEOUT_SECONDS,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
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
        try:
            result = subprocess.run(  # nosec B603
                cmd,
                capture_output=True,
                text=True,
                timeout=GITHUB_CLI_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"gh issue list timed out after {GITHUB_CLI_TIMEOUT_SECONDS} seconds"
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(f"gh command failed: {result.stderr}")
        parsed: list[dict[str, Any]] = json.loads(result.stdout)
        return parsed
