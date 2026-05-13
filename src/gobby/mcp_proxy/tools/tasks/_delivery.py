"""Delivery-state MCP tools for PR and merge orchestration."""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess  # nosec B404 # used for fixed git push/current-branch commands.
from typing import Any

import httpx

from gobby.build.delivery import normalize_github_repo, resolve_project_source_repo
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.delivery import TaskDeliveryStateManager

_GITHUB_TOKEN_ENV_NAMES = ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN")
_GITHUB_TOKEN_SECRET_NAMES = ("github_personal_access_token", "github_token", "gh_token")
logger = logging.getLogger(__name__)


def _resolve_task(ctx: RegistryContext, task_id: str) -> str:
    return resolve_task_id_for_mcp(ctx.task_manager, task_id)


def create_delivery_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create delivery-state task ops tools."""
    registry = InternalToolRegistry(
        name="gobby-tasks-delivery",
        description="PR and merge delivery state tools",
    )
    manager = TaskDeliveryStateManager(ctx.task_manager.db)

    def get_delivery_state(task_id: str) -> dict[str, Any]:
        """Read PR and merge delivery state for a task."""
        resolved_id = _resolve_task(ctx, task_id)
        return {
            "ok": True,
            "task_id": resolved_id,
            "delivery": manager.get_state(resolved_id),
        }

    registry.register(
        name="get_delivery_state",
        description="Read PR and merge delivery state for a task.",
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
        output_schema={"type": "object"},
        func=get_delivery_state,
    )

    def record_pr_state(
        task_id: str,
        unit_key: str | None = None,
        worktree_id: str | None = None,
        repo: str | None = None,
        source_branch: str | None = None,
        target_branch: str | None = None,
        pr_required: bool | None = None,
        protection: dict[str, Any] | None = None,
        pr_url: str | None = None,
        github_pr_number: int | None = None,
        gate_snapshot: dict[str, Any] | None = None,
        pr_state: str | None = None,
        local_update_attempts: int | None = None,
        merge_strategy: str | None = None,
        campaign_state: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        """Record PR delivery state without mutating the task stage."""
        resolved_id = _resolve_task(ctx, task_id)
        if campaign_state is not None or merge_strategy is not None:
            manager.record_campaign(
                resolved_id,
                state=campaign_state,
                merge_strategy=merge_strategy,
                last_error=last_error,
            )
        unit_fields = {
            "worktree_id": worktree_id,
            "repo": repo,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "pr_required": pr_required,
            "protection_json": protection,
            "pr_url": pr_url,
            "github_pr_number": github_pr_number,
            "gate_snapshot_json": gate_snapshot,
            "pr_state": pr_state,
            "local_update_attempts": local_update_attempts,
            "last_error": last_error,
        }
        if any(value is not None for value in unit_fields.values()):
            manager.record_unit(
                resolved_id,
                unit_key=unit_key,
                **unit_fields,
            )
        return {
            "ok": True,
            "task_id": resolved_id,
            "delivery": manager.get_state(resolved_id),
        }

    registry.register(
        name="record_pr_state",
        description="Record PR delivery state without mutating the task stage.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "unit_key": {"type": ["string", "null"]},
                "worktree_id": {"type": ["string", "null"]},
                "repo": {"type": ["string", "null"]},
                "source_branch": {"type": ["string", "null"]},
                "target_branch": {"type": ["string", "null"]},
                "pr_required": {"type": ["boolean", "null"]},
                "protection": {"type": ["object", "null"]},
                "pr_url": {"type": ["string", "null"]},
                "github_pr_number": {"type": ["integer", "null"]},
                "gate_snapshot": {"type": ["object", "null"]},
                "pr_state": {"type": ["string", "null"]},
                "local_update_attempts": {"type": ["integer", "null"]},
                "merge_strategy": {"type": ["string", "null"]},
                "campaign_state": {"type": ["string", "null"]},
                "last_error": {"type": ["string", "null"]},
            },
            "required": ["task_id"],
        },
        output_schema={"type": "object"},
        func=record_pr_state,
    )

    async def open_delivery_pr(
        task_id: str,
        source_branch: str | None = None,
        target_branch: str | None = None,
        unit_key: str | None = None,
        worktree_id: str | None = None,
        source_repo: str | None = None,
        target_repo: str | None = None,
        title: str | None = None,
        body: str | None = None,
        draft: bool = False,
        maintainer_can_modify: bool = True,
        push: bool = True,
        force_with_lease: bool = False,
    ) -> dict[str, Any]:
        """Push a delivery branch, reuse or open a PR, and persist PR metadata."""
        resolved_id = _resolve_task(ctx, task_id)
        task = ctx.task_manager.get_task(resolved_id)
        worktree = ctx.worktree_manager.get(worktree_id) if worktree_id else None
        artifacts = ctx.task_manager.artifacts.get_artifacts(resolved_id)
        project_id = getattr(task, "project_id", None) or ctx.get_current_project_id()
        if project_id is None:
            raise ValueError("Could not resolve project_id for delivery PR")

        delivery = TaskDeliveryStateManager(ctx.task_manager.db)
        state = delivery.get_state(resolved_id)
        campaign = state["campaign"] or {}
        try:
            effective_source_repo = normalize_github_repo(
                source_repo
                or campaign.get("source_repo")
                or resolve_project_source_repo(ctx.task_manager.db, project_id)
            )
            effective_target_repo = normalize_github_repo(
                target_repo or campaign.get("target_repo") or effective_source_repo
            )
            effective_source_branch = _resolve_source_branch(
                source_branch=source_branch,
                worktree=worktree,
                repo_path=_repo_path(ctx, project_id, worktree),
            )
            effective_target_branch = (
                target_branch
                or getattr(worktree, "base_branch", None)
                or artifacts.target_branch
                or "main"
            )
            effective_unit_key = unit_key or _derive_delivery_unit_key(
                worktree_id=worktree_id,
                source_branch=effective_source_branch,
                target_branch=effective_target_branch,
            )
            existing = _existing_delivery_unit(
                state,
                effective_unit_key,
                source_branch=effective_source_branch,
                target_branch=effective_target_branch,
            )
            if existing and existing.get("pr_url"):
                return {
                    "ok": True,
                    "task_id": resolved_id,
                    "pr_url": existing["pr_url"],
                    "github_pr_number": existing.get("github_pr_number"),
                    "delivery_unit": existing,
                    "idempotent": True,
                    "reused": True,
                    "created_via": "delivery_state",
                    "pushed": False,
                }

            repo_path = _repo_path(ctx, project_id, worktree)
            pushed = False
            if push:
                _push_branch(
                    repo_path=repo_path,
                    source_branch=effective_source_branch,
                    remote_branch=effective_source_branch,
                    force_with_lease=force_with_lease,
                )
                pushed = True

            task_title = title or str(getattr(task, "title", None) or "Gobby delivery")
            task_body = body if body is not None else str(getattr(task, "description", None) or "")
            pr_payload, created_via, reused = await _open_or_reuse_github_pr(
                ctx=ctx,
                db=ctx.task_manager.db,
                source_repo=effective_source_repo,
                target_repo=effective_target_repo,
                source_branch=effective_source_branch,
                target_branch=effective_target_branch,
                title=task_title,
                body=task_body,
                draft=draft,
                maintainer_can_modify=maintainer_can_modify,
            )
            pr_url = _pr_url(pr_payload)
            pr_number = _pr_number(pr_payload)
            unit = delivery.record_unit(
                resolved_id,
                unit_key=effective_unit_key,
                worktree_id=worktree_id,
                repo=effective_target_repo,
                source_branch=effective_source_branch,
                target_branch=effective_target_branch,
                pr_required=True,
                pr_url=pr_url,
                github_pr_number=pr_number,
                pr_state="open",
                last_error=None,
            )
            delivery.record_campaign(
                resolved_id,
                delivery_mode="pull_request",
                source_repo=effective_source_repo,
                target_repo=effective_target_repo,
                state="pr_open",
                last_error=None,
            )
            return {
                "ok": True,
                "task_id": resolved_id,
                "source_repo": effective_source_repo,
                "target_repo": effective_target_repo,
                "source_branch": effective_source_branch,
                "target_branch": effective_target_branch,
                "pr_url": pr_url,
                "github_pr_number": pr_number,
                "delivery_unit": unit,
                "idempotent": reused,
                "reused": reused,
                "created_via": created_via,
                "pushed": pushed,
            }
        except Exception as exc:
            message = str(exc)
            delivery.record_campaign(resolved_id, state="blocked", last_error=message)
            if source_branch or target_branch or worktree_id:
                delivery.record_unit(
                    resolved_id,
                    unit_key=unit_key,
                    worktree_id=worktree_id,
                    repo=target_repo,
                    source_branch=source_branch,
                    target_branch=target_branch,
                    last_error=message,
                )
            return {"ok": False, "task_id": resolved_id, "error": message}

    registry.register(
        name="open_delivery_pr",
        description="Push/reuse/open a delivery pull request and persist PR metadata.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "source_branch": {"type": ["string", "null"]},
                "target_branch": {"type": ["string", "null"]},
                "unit_key": {"type": ["string", "null"]},
                "worktree_id": {"type": ["string", "null"]},
                "source_repo": {"type": ["string", "null"]},
                "target_repo": {"type": ["string", "null"]},
                "title": {"type": ["string", "null"]},
                "body": {"type": ["string", "null"]},
                "draft": {"type": "boolean", "default": False},
                "maintainer_can_modify": {"type": "boolean", "default": True},
                "push": {"type": "boolean", "default": True},
                "force_with_lease": {"type": "boolean", "default": False},
            },
            "required": ["task_id"],
        },
        output_schema={"type": "object"},
        func=open_delivery_pr,
    )

    return registry


def _derive_delivery_unit_key(
    *,
    worktree_id: str | None,
    source_branch: str,
    target_branch: str,
) -> str:
    if worktree_id:
        return f"worktree:{worktree_id}"
    return f"branch:{source_branch}->{target_branch}"


def _existing_delivery_unit(
    state: dict[str, Any],
    unit_key: str,
    *,
    source_branch: str,
    target_branch: str,
) -> dict[str, Any] | None:
    for unit in state.get("units") or []:
        if isinstance(unit, dict) and unit.get("unit_key") == unit_key:
            return dict(unit)
    for unit in state.get("units") or []:
        if (
            isinstance(unit, dict)
            and unit.get("pr_url")
            and unit.get("source_branch") == source_branch
            and unit.get("target_branch") == target_branch
        ):
            return dict(unit)
    return None


def _repo_path(ctx: RegistryContext, project_id: str, worktree: Any | None) -> str:
    if worktree is not None and getattr(worktree, "worktree_path", None):
        return str(worktree.worktree_path)
    project = ctx.project_manager.get(project_id)
    if project is None or project.repo_path is None:
        raise ValueError("delivery PR push requires a project repo_path or worktree_id")
    return project.repo_path


def _resolve_source_branch(
    *,
    source_branch: str | None,
    worktree: Any | None,
    repo_path: str,
) -> str:
    if source_branch:
        return source_branch
    if worktree is not None and getattr(worktree, "branch_name", None):
        return str(worktree.branch_name)
    result = subprocess.run(  # nosec B603 B607 # fixed git command.
        ["git", "branch", "--show-current"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    branch = result.stdout.strip() if result.returncode == 0 else ""
    if not branch:
        raise ValueError("source_branch is required when current branch cannot be resolved")
    return branch


def _push_branch(
    *,
    repo_path: str,
    source_branch: str,
    remote_branch: str,
    force_with_lease: bool,
) -> None:
    _validate_branch_ref(repo_path, source_branch, label="source")
    _validate_branch_ref(repo_path, remote_branch, label="remote")
    command = ["git", "push", "--no-verify"]
    if force_with_lease:
        command.append("--force-with-lease")
    command.extend(["origin", f"{source_branch}:{remote_branch}"])
    result = subprocess.run(  # nosec B603 B607 # fixed git command with validated args.
        command,
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git push failed: {result.stderr.strip() or result.stdout.strip()}")


def _validate_branch_ref(repo_path: str, branch: str, *, label: str) -> None:
    result = subprocess.run(  # nosec B603 B607 # fixed git command with validated args.
        ["git", "check-ref-format", "--branch", branch],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise ValueError(f"{label}_branch is not a valid git branch ref: {branch!r}{suffix}")


async def _open_or_reuse_github_pr(
    *,
    ctx: RegistryContext,
    db: Any,
    source_repo: str,
    target_repo: str,
    source_branch: str,
    target_branch: str,
    title: str,
    body: str,
    draft: bool,
    maintainer_can_modify: bool,
) -> tuple[dict[str, Any], str, bool]:
    target_owner, target_name = _split_repo(target_repo)
    source_owner, source_name = _split_repo(source_repo)
    head = source_branch if source_repo == target_repo else f"{source_owner}:{source_branch}"

    existing = await _find_existing_pr(ctx, target_owner, target_name, head, target_branch)
    if existing is not None:
        return existing, "github_mcp", True

    if source_owner == target_owner and source_name != target_name:
        return (
            await _create_pull_request_rest(
                db=db,
                owner=target_owner,
                repo=target_name,
                title=title,
                body=body,
                head=head,
                head_repo=source_name,
                base=target_branch,
                draft=draft,
                maintainer_can_modify=maintainer_can_modify,
            ),
            "github_rest",
            False,
        )

    mcp_manager = getattr(ctx, "mcp_manager", None)
    if mcp_manager is None:
        raise RuntimeError("GitHub MCP manager is required to create this pull request")
    created = await mcp_manager.call_tool(
        server_name="github",
        tool_name="create_pull_request",
        arguments={
            "owner": target_owner,
            "repo": target_name,
            "title": title,
            "body": body,
            "head": head,
            "base": target_branch,
            "draft": draft,
            "maintainer_can_modify": maintainer_can_modify,
        },
    )
    return _unwrap_github_payload(created), "github_mcp", False


async def _find_existing_pr(
    ctx: RegistryContext,
    owner: str,
    repo: str,
    head: str,
    base: str,
) -> dict[str, Any] | None:
    mcp_manager = getattr(ctx, "mcp_manager", None)
    if mcp_manager is None:
        return None
    result = await mcp_manager.call_tool(
        server_name="github",
        tool_name="list_pull_requests",
        arguments={
            "owner": owner,
            "repo": repo,
            "state": "open",
            "head": head,
            "base": base,
            "per_page": 10,
        },
    )
    payload = _unwrap_github_payload(result)
    pulls = payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        pulls = payload.get("pull_requests", [])
    if not isinstance(pulls, list) or not pulls:
        return None
    first = pulls[0]
    return first if isinstance(first, dict) else None


async def _create_pull_request_rest(
    *,
    db: Any,
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    head_repo: str,
    base: str,
    draft: bool,
    maintainer_can_modify: bool,
) -> dict[str, Any]:
    token = _github_token(db)
    if not token:
        raise RuntimeError("GitHub token is required for same-organization cross-repo PRs")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    payload = {
        "title": title,
        "body": body,
        "head": head,
        "head_repo": head_repo,
        "base": base,
        "draft": draft,
        "maintainer_can_modify": maintainer_can_modify,
    }
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=headers, json=payload)
    if response.status_code >= 400:
        raise RuntimeError(f"GitHub REST create PR failed: {response.status_code} {response.text}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("GitHub REST create PR returned an unexpected payload")
    return data


def _github_token(db: Any) -> str | None:
    for name in _GITHUB_TOKEN_ENV_NAMES:
        token = os.environ.get(name)
        if token:
            logger.debug("Using GitHub token from environment variable %s", name)
            return token
    logger.debug("No GitHub token found in environment; checking stored secrets")
    try:
        from gobby.storage.secrets import SecretStore

        store = SecretStore(db)
        for name in _GITHUB_TOKEN_SECRET_NAMES:
            token = store.get(name)
            if token:
                logger.debug("Using GitHub token from secret store entry %s", name)
                return token
    except (AttributeError, LookupError, OSError, RuntimeError, sqlite3.Error) as exc:
        logger.debug("GitHub token lookup from secret store failed: %s", exc, exc_info=True)
        return None
    logger.debug("No GitHub token found in environment or stored secrets")
    return None


def _unwrap_github_payload(result: Any) -> Any:
    if isinstance(result, dict) and "success" in result and "result" in result:
        return result["result"]
    return result


def _split_repo(repo: str) -> tuple[str, str]:
    owner, name = normalize_github_repo(repo).split("/", 1)
    return owner, name


def _pr_url(payload: dict[str, Any]) -> str:
    value = payload.get("html_url") or payload.get("url")
    if not isinstance(value, str) or not value:
        raise RuntimeError("GitHub PR response did not include a PR URL")
    return value


def _pr_number(payload: dict[str, Any]) -> int | None:
    value = payload.get("number")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
