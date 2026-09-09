"""
File browser routes for Gobby HTTP server.

Provides file tree browsing, reading, and image serving endpoints.
"""

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from gobby.servers.routes.projects import (
    HIDDEN_PROJECT_NAMES,
    _checkout_http_error,
    _projects_to_responses,
)
from gobby.storage.project_checkouts import (
    CheckoutNotFoundError,
    CheckoutSentinelRejectedError,
    MissingMachineContextError,
    require_root,
)
from gobby.storage.projects import PERSONAL_PROJECT_ID, LocalProjectManager
from gobby.storage.workspace_machine_scope import (
    MachineOwnershipMismatchError,
    require_local_machine_id,
)
from gobby.utils.daemon_git import GitOk, GitResult, daemon_git, parse_porcelain_v1_z

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

# Max file size to read (1MB default)
DEFAULT_MAX_SIZE = 1_048_576
MAX_READ_SIZE = 16 * DEFAULT_MAX_SIZE
GIT_PORCELAIN_STATUS_MIN_LINE_LENGTH = 4
GIT_PORCELAIN_PATH_OFFSET = 3

# Extensions treated as images
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp"}

# Extensions treated as binary (skip reading content)
BINARY_EXTENSIONS = {
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".o",
    ".a",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".mp3",
    ".mp4",
    ".wav",
    ".avi",
    ".mov",
    ".mkv",
    ".flac",
    ".pyc",
    ".class",
    ".wasm",
}


def _resolve_safe_path(project_path: str, relative_path: str) -> Path:
    """Resolve a relative path within a project, preventing path traversal.

    Args:
        project_path: Absolute path to the project root.
        relative_path: Relative path within the project.

    Returns:
        Resolved absolute Path.

    Raises:
        HTTPException: If path traversal is detected.
    """
    base = Path(project_path).resolve()
    target = (base / relative_path).resolve()
    if not target.is_relative_to(base):
        raise HTTPException(403, "Path traversal not allowed")
    return target


def _read_prefix(path: Path, byte_limit: int) -> bytes:
    """Read at most ``byte_limit`` bytes from a file."""
    with path.open("rb") as handle:
        return handle.read(byte_limit)


def _get_project_manager(server: "HTTPServer") -> LocalProjectManager:
    """Get a LocalProjectManager from the server's database."""
    if server.session_manager is None:
        raise HTTPException(503, "Session manager not available")
    return LocalProjectManager(server.session_manager.db)


def _require_project_checkout_root(server: "HTTPServer", project_id: str) -> str:
    """Return this daemon's primary checkout root, or raise HTTP 404/409."""
    pm = _get_project_manager(server)
    project = pm.get(project_id)
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    try:
        machine_id = require_local_machine_id(
            None, resource_kind="project_checkout", resource_id=project_id
        )
        return require_root(pm.db, project_id, machine_id)
    except (
        CheckoutNotFoundError,
        MissingMachineContextError,
        MachineOwnershipMismatchError,
        CheckoutSentinelRejectedError,
    ) as exc:
        raise _checkout_http_error(exc) from exc


async def _run_git(cwd: str, args: list[str], timeout: float = 10.0) -> GitResult:
    """Run a git command asynchronously."""
    return await daemon_git.run(args, cwd=cwd, timeout=timeout)


async def _get_git_tracked_files(project_path: str) -> set[str] | None:
    """Get set of git-tracked files, or None if not a git repo."""
    try:
        tracked, untracked = await asyncio.gather(
            _run_git(project_path, ["ls-files"]),
            _run_git(project_path, ["ls-files", "--others", "--exclude-standard"]),
        )
    except OSError:
        return None
    if not isinstance(tracked, GitOk) or not isinstance(untracked, GitOk):
        return None

    files = set(tracked.stdout.strip().splitlines())
    files.update(untracked.stdout.strip().splitlines())
    files.discard("")
    return files


def _is_path_visible(
    relative_path: str,
    git_files: set[str] | None,
    is_dir: bool,
) -> bool:
    """Check if a path should be visible in the file tree.

    Filters out .git directory and respects .gitignore via git ls-files.
    """
    parts = Path(relative_path).parts
    # Always hide .git directory
    if ".git" in parts:
        return False

    if git_files is None:
        # No git info — show everything except .git
        return True

    if is_dir:
        # Show directory if any tracked file is inside it
        prefix = relative_path.rstrip("/") + "/"
        return any(f.startswith(prefix) for f in git_files)

    return relative_path in git_files


def create_files_router(server: "HTTPServer") -> APIRouter:
    """Create files router with endpoints bound to server instance."""
    router = APIRouter(prefix="/api/files", tags=["files"])

    @router.get("/projects")
    async def list_projects() -> list[dict[str, Any]]:
        """List visible projects as checkout-shaped JSON, never `repo_path`.

        Hidden system projects stay out of the browser; stats and this daemon's
        checkouts resolve in one batch per request rather than per project.
        """
        pm = _get_project_manager(server)
        projects = await server.run_db(pm.list)
        if not any(project.id == PERSONAL_PROJECT_ID for project in projects):
            personal = await server.run_db(pm.get, PERSONAL_PROJECT_ID)
            if personal is not None:
                projects = [personal, *projects]
        visible = [project for project in projects if project.name not in HIDDEN_PROJECT_NAMES]
        results: list[dict[str, Any]] = await server.run_db(_projects_to_responses, server, visible)
        return results

    @router.get("/tree")
    async def list_directory(
        project_id: str = Query(..., description="Project ID"),
        path: str = Query("", description="Relative path within project"),
    ) -> list[dict[str, Any]]:
        """List directory contents for the file tree.

        Returns entries sorted: directories first, then files, both alphabetical.
        Respects .gitignore via git ls-files.
        """
        repo_path = await server.run_db(_require_project_checkout_root, server, project_id)
        target = _resolve_safe_path(repo_path, path)
        if not target.is_dir():
            raise HTTPException(400, "Path is not a directory")

        # Get git-tracked files for filtering
        git_files = await _get_git_tracked_files(repo_path)

        def _scan_dir() -> list[dict[str, Any]]:
            entries: list[dict[str, Any]] = []
            try:
                for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                    rel = str(child.relative_to(Path(repo_path).resolve()))
                    is_dir = child.is_dir()

                    if not _is_path_visible(rel, git_files, is_dir):
                        continue

                    entry: dict[str, Any] = {
                        "name": child.name,
                        "path": rel,
                        "is_dir": is_dir,
                    }
                    if not is_dir:
                        entry["size"] = child.stat().st_size
                        entry["extension"] = child.suffix.lower()
                    entries.append(entry)
                return entries
            except PermissionError as e:
                raise HTTPException(403, "Permission denied") from e

        # Offload blocking IO
        entries = await asyncio.get_running_loop().run_in_executor(None, _scan_dir)

        # Sort: directories first, then files, alphabetical within each group
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return entries

    @router.get("/read")
    async def read_file(
        project_id: str = Query(..., description="Project ID"),
        path: str = Query(..., description="Relative path within project"),
        max_size: int = Query(
            DEFAULT_MAX_SIZE,
            ge=0,
            le=MAX_READ_SIZE,
            description="Max bytes to read",
        ),
    ) -> dict[str, Any]:
        """Read file content.

        Returns content with metadata. Large files are truncated.
        Binary files return metadata only.
        """
        repo_path = await server.run_db(_require_project_checkout_root, server, project_id)
        target = _resolve_safe_path(repo_path, path)
        if not target.is_file():
            raise HTTPException(404, "File not found")

        stat = target.stat()
        extension = target.suffix.lower()
        mime_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"

        # Check if image
        is_image = extension in IMAGE_EXTENSIONS

        # Check if binary
        is_binary = extension in BINARY_EXTENSIONS or is_image

        result: dict[str, Any] = {
            "size": stat.st_size,
            "truncated": False,
            "binary": is_binary,
            "image": is_image,
            "mime_type": mime_type,
            "extension": extension,
            "name": target.name,
        }

        if is_binary:
            result["content"] = None
            return result

        # Read text content (async)
        try:
            loop = asyncio.get_running_loop()
            raw = await loop.run_in_executor(None, _read_prefix, target, max_size + 1)
            truncated = len(raw) > max_size
            if truncated:
                raw = raw[:max_size]
            content = raw.decode("utf-8", errors="replace")
            result["content"] = content
            result["truncated"] = truncated
        except OSError as e:
            logger.error("Failed to read file: %s (project_id=%s)", e, project_id)
            raise HTTPException(500, f"Failed to read file: {e}") from e

        return result

    @router.get("/image")
    async def serve_image(
        project_id: str = Query(..., description="Project ID"),
        path: str = Query(..., description="Relative path within project"),
    ) -> FileResponse:
        """Serve an image file directly for <img> tags."""
        repo_path = await server.run_db(_require_project_checkout_root, server, project_id)
        target = _resolve_safe_path(repo_path, path)
        if not target.is_file():
            raise HTTPException(404, "File not found")

        extension = target.suffix.lower()
        if extension not in IMAGE_EXTENSIONS:
            raise HTTPException(400, "Not an image file")

        mime_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        headers = None
        if extension == ".svg":
            headers = {
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
            }
        return FileResponse(target, media_type=mime_type, headers=headers)

    class WriteFileRequest(BaseModel):
        project_id: str
        path: str
        content: str

    @router.post("/write")
    async def write_file(request: WriteFileRequest) -> dict[str, Any]:
        """Write content to a file.

        Refuses writes to .git/ directory.
        """
        repo_path = await server.run_db(_require_project_checkout_root, server, request.project_id)
        target = _resolve_safe_path(repo_path, request.path)

        # Refuse writes to .git/
        rel = str(target.relative_to(Path(repo_path).resolve()))
        if rel.startswith(".git") and (rel == ".git" or rel.startswith(".git/")):
            raise HTTPException(403, "Cannot write to .git directory")

        if not target.parent.exists():
            raise HTTPException(404, "Parent directory does not exist")

        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: target.write_text(request.content, encoding="utf-8")
            )
        except OSError as e:
            raise HTTPException(500, f"Failed to write file: {e}") from e

        return {
            "success": True,
            "size": len(request.content.encode("utf-8")),
            "path": request.path,
        }

    @router.get("/git-status")
    async def git_status(
        project_id: str = Query(..., description="Project ID"),
    ) -> dict[str, Any]:
        """Get git status for a project.

        Returns branch name and file statuses using `git status --porcelain`.
        Status codes: M=modified, A=added, D=deleted, ?=untracked, R=renamed.
        """
        repo_path = await server.run_db(_require_project_checkout_root, server, project_id)

        branch_result, status_result = await asyncio.gather(
            _run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5),
            daemon_git.status(repo_path, timeout=10),
            return_exceptions=True,
        )
        if isinstance(branch_result, OSError):
            raise HTTPException(503, f"Git status is unavailable: {branch_result}")
        if isinstance(branch_result, BaseException):
            raise branch_result
        if not isinstance(branch_result, GitOk) or not isinstance(status_result, GitOk):
            raise HTTPException(503, "Git status is unavailable")

        files = {
            entry.path: entry.code.strip() or "?"
            for entry in parse_porcelain_v1_z(status_result.stdout)
        }
        return {"branch": branch_result.stdout.strip(), "files": files}

    @router.get("/git-diff")
    async def git_diff(
        project_id: str = Query(..., description="Project ID"),
        path: str = Query(..., description="Relative file path"),
    ) -> dict[str, str]:
        """Get git diff for a specific file."""
        repo_path = await server.run_db(_require_project_checkout_root, server, project_id)

        # Validate path
        _resolve_safe_path(repo_path, path)

        result = await _run_git(
            repo_path,
            ["--literal-pathspecs", "diff", "HEAD", "--", path],
            timeout=10,
        )
        if not isinstance(result, GitOk):
            raise HTTPException(503, "Git diff is unavailable")
        return {"diff": result.stdout, "path": path}

    return router
