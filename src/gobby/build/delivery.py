"""Build delivery campaign helpers."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from gobby.build.options import BuildOptions
from gobby.storage.database import DatabaseProtocol
from gobby.storage.delivery import TaskDeliveryStateManager
from gobby.storage.projects import LocalProjectManager
from gobby.utils.git import get_github_url

logger = logging.getLogger(__name__)

_GITHUB_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def record_build_delivery_campaign(
    db: DatabaseProtocol,
    *,
    project_id: str,
    task_id: str,
    opts: BuildOptions,
) -> None:
    """Persist delivery campaign metadata resolved from a build profile."""
    if opts.delivery_mode != "pull_request":
        return

    source_repo = resolve_project_source_repo(db, project_id)
    target_repo = (
        normalize_github_repo(opts.delivery_target_repo)
        if opts.delivery_target_repo
        else source_repo
    )
    TaskDeliveryStateManager(db).record_campaign(
        task_id,
        delivery_mode=opts.delivery_mode,
        source_repo=source_repo,
        target_repo=target_repo,
        state="pending",
    )
    logger.info(
        "Recorded build delivery campaign",
        extra={
            "task_id": task_id,
            "project_id": project_id,
            "source_repo": source_repo,
            "target_repo": target_repo,
        },
    )


def resolve_project_source_repo(db: DatabaseProtocol, project_id: str) -> str:
    """Resolve the GitHub source repo for a project as owner/repo."""
    project = LocalProjectManager(db).get(project_id)
    if project is None:
        raise ValueError(f"project {project_id!r} not found")
    if project.github_repo:
        return normalize_github_repo(project.github_repo)
    if project.github_url:
        parsed = github_repo_from_url(project.github_url)
        if parsed:
            return parsed
    if project.repo_path:
        remote_url = get_github_url(Path(project.repo_path))
        if remote_url:
            parsed = github_repo_from_url(remote_url)
            if parsed:
                return parsed
    raise ValueError("pull_request delivery requires project github_repo, github_url, or origin")


def normalize_github_repo(repo: str | None) -> str:
    """Validate and normalize an owner/repo string."""
    if repo is None:
        raise ValueError("GitHub repo is required")
    stripped = repo.strip()
    owner, separator, name = stripped.partition("/")
    if (
        not separator
        or not owner
        or not name
        or "/" in name
        or owner.strip() != owner
        or name.strip() != name
        or not _is_valid_github_owner(owner)
        or not _is_valid_github_repo_name(name)
    ):
        raise ValueError(f"Invalid GitHub repo {repo!r}; expected 'owner/repo'")
    return f"{owner}/{name}"


def github_repo_from_url(url: str) -> str | None:
    """Parse common GitHub HTTPS and SSH remotes into owner/repo."""
    if url.startswith("git@github.com:"):
        path = url.removeprefix("git@github.com:")
        return _repo_from_path(path)

    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com":
        return None
    return _repo_from_path(parsed.path.lstrip("/"))


def _repo_from_path(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) < 2:
        return None
    owner = parts[0]
    name = parts[1].removesuffix(".git")
    if not owner or not name:
        return None
    try:
        return normalize_github_repo(f"{owner}/{name}")
    except ValueError:
        return None


def _is_valid_github_owner(owner: str) -> bool:
    return bool(_GITHUB_OWNER_RE.fullmatch(owner))


def _is_valid_github_repo_name(name: str) -> bool:
    if name in {".", ".."} or name.endswith(".git"):
        return False
    return bool(_GITHUB_REPO_RE.fullmatch(name))
