"""Helpers for probing GitHub branch protection during merge delivery."""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import subprocess  # nosec B404 # used for a fixed git dry-run fallback.
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.worktrees.git import WorktreeGitManager

_GITHUB_TOKEN_ENV_NAMES = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
)
_GITHUB_TOKEN_SECRET_NAMES = (
    "github_personal_access_token",
    "github_token",
    "gh_token",
)
_PROTECTED_PUSH_MARKERS = (
    "protected branch hook declined",
    "protected branch",
    "branch is protected",
    "required status check",
    "required status checks",
    "pull request",
    "pre-receive hook declined",
    "gh006",
)
_PROTECTION_PROBE_TIMEOUT_SECONDS = 30


def parse_github_remote(remote_url: str) -> tuple[str, str] | None:
    ssh_match = re.match(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?$", remote_url)
    if ssh_match:
        return ssh_match.group("owner"), ssh_match.group("repo")

    parsed = urlparse(remote_url)
    if parsed.netloc.lower() != "github.com":
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return None
    repo = parts[1].removesuffix(".git")
    return parts[0], repo


def github_token(db: HubDatabase | None) -> str | None:
    for name in _GITHUB_TOKEN_ENV_NAMES:
        token = os.environ.get(name)
        if token:
            return token
    if db is None:
        return None
    try:
        from gobby.storage.secrets import SecretStore

        store = SecretStore(db)
        for name in _GITHUB_TOKEN_SECRET_NAMES:
            token = store.get(name)
            if token:
                return token
    except (LookupError, OSError, RuntimeError, sqlite3.Error):
        return None
    return None


def protection_payload(
    *,
    owner: str,
    repo: str,
    branch: str,
    source: str,
    requires_pr: bool,
    requires_status_checks: list[str] | None = None,
    requires_up_to_date: bool = False,
    requires_review_count: int = 0,
    protection_unknown: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "success": True,
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "source": source,
        "requires_pr": requires_pr,
        "requires_status_checks": requires_status_checks or [],
        "requires_up_to_date": requires_up_to_date,
        "requires_review_count": requires_review_count,
        "protection_unknown": protection_unknown,
        "error": error,
    }


def git_output(result: Any) -> str:
    return (result.stderr or result.stdout or "").strip()


def parse_protection_response(
    owner: str,
    repo: str,
    branch: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    status_checks = payload.get("required_status_checks") or {}
    contexts = list(status_checks.get("contexts") or [])
    for check in status_checks.get("checks") or []:
        context = check.get("context") if isinstance(check, dict) else None
        if context:
            contexts.append(context)
    review_rule = payload.get("required_pull_request_reviews") or {}
    return protection_payload(
        owner=owner,
        repo=repo,
        branch=branch,
        source="github_api",
        requires_pr=True,
        requires_status_checks=sorted(set(contexts)),
        requires_up_to_date=bool(status_checks.get("strict")),
        requires_review_count=int(review_rule.get("required_approving_review_count") or 0),
    )


async def push_dry_run_probe(
    *,
    repo_path: str,
    owner: str,
    repo: str,
    branch: str,
    git_manager: WorktreeGitManager | None,
    source: str,
    error: str | None,
) -> dict[str, Any]:
    command = ["push", "--dry-run", "origin", f"HEAD:{branch}"]
    if git_manager is not None:
        result = await asyncio.to_thread(
            git_manager.run_git_command,
            command,
            cwd=repo_path,
            timeout=_PROTECTION_PROBE_TIMEOUT_SECONDS,
        )
        returncode = result.returncode
        output = f"{result.stdout}\n{result.stderr}"
    else:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["git", *command],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=_PROTECTION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        returncode = proc.returncode
        output = f"{proc.stdout}\n{proc.stderr}"

    lowered = output.lower()
    looks_protected = any(marker in lowered for marker in _PROTECTED_PUSH_MARKERS)
    if returncode == 0:
        requires_pr = False
        protection_unknown = False
    else:
        requires_pr = True
        protection_unknown = not looks_protected
    return protection_payload(
        owner=owner,
        repo=repo,
        branch=branch,
        source=source,
        requires_pr=requires_pr,
        protection_unknown=protection_unknown,
        error=error or (output.strip() if returncode != 0 else None),
    )
