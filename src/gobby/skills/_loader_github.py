"""GitHub import helpers for skill loading."""

from __future__ import annotations

import re
import subprocess  # nosec B404 # required for git clone/pull operations with validated input
from pathlib import Path

from gobby.skills._loader_models import GitHubRef, SkillLoadError

# Default cache directory for cloned GitHub repos
DEFAULT_CACHE_DIR = Path.home() / ".gobby" / "skill-cache"


def parse_github_url(url: str) -> GitHubRef:
    """Parse a GitHub URL into its components."""
    if not url or not url.strip():
        raise ValueError("Invalid GitHub URL: empty string")

    url = url.strip()

    if url.startswith("github:"):
        url = url[7:]
        return _parse_owner_repo_format(url)

    if url.startswith("https://github.com/") or url.startswith("http://github.com/"):
        return _parse_full_github_url(url)

    if _looks_like_bare_github_ref(url):
        return _parse_owner_repo_format(url)

    raise ValueError(f"Invalid GitHub URL: {url}")


def _looks_like_bare_github_ref(url: str) -> bool:
    candidate = url.split("#", maxsplit=1)[0]
    if candidate.startswith(("/", "./", "../", "~")) or "\\" in candidate:
        return False
    parts = candidate.split("/")
    if len(parts) != 2:
        return False
    owner, repo = parts
    if owner in {".", ".."} or repo in {".", ".."}:
        return False
    safe_name_pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
    return bool(
        owner and repo and safe_name_pattern.fullmatch(owner) and safe_name_pattern.fullmatch(repo)
    )


def _parse_owner_repo_format(url: str) -> GitHubRef:
    """Parse owner/repo#branch format."""
    branch = None

    if "#" in url:
        url, branch = url.rsplit("#", 1)

    parts = url.split("/")
    if len(parts) != 2:
        raise ValueError(f"Invalid GitHub URL: {url}")

    owner = parts[0]
    repo = parts[1].removesuffix(".git")

    return GitHubRef(owner=owner, repo=repo, branch=branch)


def _parse_full_github_url(url: str) -> GitHubRef:
    """Parse full https://github.com/... URL."""
    url = re.sub(r"^https?://github\.com/", "", url)
    url = url.rstrip("/")
    url = url.removesuffix(".git")

    parts = url.split("/")
    if len(parts) < 2:
        raise ValueError(f"Invalid GitHub URL: {url}")

    owner = parts[0]
    repo = parts[1]
    branch = None
    path = None

    if len(parts) > 2 and parts[2] == "tree":
        if len(parts) > 3:
            branch = parts[3]
        if len(parts) > 4:
            path = "/".join(parts[4:])

    return GitHubRef(owner=owner, repo=repo, branch=branch, path=path)


def _validate_github_ref(ref: GitHubRef) -> None:
    """Validate GitHub reference components for safety."""
    safe_name_pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
    safe_branch_pattern = re.compile(r"^[A-Za-z0-9_./][A-Za-z0-9_./-]*$")

    if not ref.owner or len(ref.owner) > 100:
        raise SkillLoadError(f"Invalid GitHub owner: {ref.owner}")
    if not safe_name_pattern.match(ref.owner):
        raise SkillLoadError(f"Invalid characters in GitHub owner: {ref.owner}")

    if not ref.repo or len(ref.repo) > 100:
        raise SkillLoadError(f"Invalid GitHub repo: {ref.repo}")
    if not safe_name_pattern.match(ref.repo):
        raise SkillLoadError(f"Invalid characters in GitHub repo: {ref.repo}")

    if ref.branch:
        if len(ref.branch) > 200:
            raise SkillLoadError(f"Branch name too long: {ref.branch}")
        if not safe_branch_pattern.match(ref.branch):
            raise SkillLoadError(f"Invalid characters in branch name: {ref.branch}")
        if ".." in ref.branch or any(
            c in ref.branch for c in ("$", "`", ";", "&", "|", "<", ">", "\\", "\n", "\r")
        ):
            raise SkillLoadError(f"Invalid branch name: {ref.branch}")

    _validate_github_path(ref.path)


def _validate_github_path(path: str | None) -> None:
    """Validate a repository-relative GitHub skill path."""
    if path is None:
        return

    segments = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(not segment or segment in {".", ".."} for segment in segments)
    ):
        raise SkillLoadError(f"Invalid GitHub skill path: {path}")


def resolve_github_skill_path(repo_path: Path, path: str | None) -> Path:
    """Resolve a skill path and require it to remain within its repository."""
    _validate_github_path(path)
    resolved_repo = repo_path.resolve()
    resolved_skill = (resolved_repo / path).resolve() if path else resolved_repo
    try:
        resolved_skill.relative_to(resolved_repo)
    except ValueError as exc:
        raise SkillLoadError(f"GitHub skill path escapes repository: {path}") from exc
    return resolved_skill


def clone_skill_repo(
    ref: GitHubRef,
    cache_dir: Path | None = None,
) -> Path:
    """Clone or update a GitHub repository."""
    _validate_github_ref(ref)

    cache_dir = cache_dir or DEFAULT_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    repo_path = cache_dir / ref.owner / ref.repo
    is_existing = repo_path.exists() and (repo_path / ".git").exists()

    if is_existing:
        if ref.branch:
            checkout_cmd = ["git", "-C", str(repo_path), "checkout", ref.branch]
            result = subprocess.run(  # nosec B603 # hardcoded git command, input validated
                checkout_cmd, capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                raise SkillLoadError(
                    f"Failed to checkout branch {ref.branch}: {result.stderr}",
                    ref.clone_url,
                )
        pull_cmd = ["git", "-C", str(repo_path), "pull", "--ff-only"]
        result = subprocess.run(  # nosec B603 # hardcoded git command
            pull_cmd, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise SkillLoadError(
                f"Failed to pull repository updates: {result.stderr}",
                ref.clone_url,
            )
        return repo_path

    repo_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth", "1"]
    if ref.branch:
        cmd.extend(["--branch", ref.branch])
    cmd.extend([ref.clone_url, str(repo_path)])

    result = subprocess.run(  # nosec B603 # hardcoded git clone, input validated
        cmd, capture_output=True, text=True, timeout=120
    )

    if result.returncode != 0:
        raise SkillLoadError(
            f"Failed to clone repository: {result.stderr}",
            ref.clone_url,
        )

    return repo_path
