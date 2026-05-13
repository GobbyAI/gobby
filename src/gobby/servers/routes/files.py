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

from gobby.storage.projects import LocalProjectManager

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

# Max file size to read (1MB default)
DEFAULT_MAX_SIZE = 1_048_576
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


def _get_project_manager(server: "HTTPServer") -> LocalProjectManager:
    """Get a LocalProjectManager from the server's database."""
    if server.session_manager is None:
        raise HTTPException(503, "Session manager not available")
    return LocalProjectManager(server.session_manager.db)


async def _run_git(cwd: str, args: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Run a git command asynchronously."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise

        return proc.returncode or 0, stdout.decode("utf-8", errors="replace")
    except (TimeoutError, OSError):
        return 1, ""


async def _get_git_tracked_files(project_path: str) -> set[str] | None:
    """Get set of git-tracked files, or None if not a git repo."""
    try:
        # Get tracked files
        rc, stdout = await _run_git(project_path, ["ls-files"])
        if rc != 0:
            return None

        # Get untracked but not ignored files
        rc2, stdout2 = await _run_git(project_path, ["ls-files", "--others", "--exclude-standard"])

        files: set[str] = set()
        for line in stdout.strip().splitlines():
            if line:
                files.add(line)
        if rc2 == 0:
            for line in stdout2.strip().splitlines():
                if line:
                    files.add(line)
        return files
    except Exception:
        return None


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
        """List all registered projects."""
        from gobby.storage.projects import PERSONAL_PROJECT_ID

        pm = _get_project_manager(server)
        projects = pm.list()
        result: list[dict[str, Any]] = [
            {
                "id": p.id,
                "name": p.name,
                "repo_path": p.repo_path,
            }
            for p in projects
            if p.repo_path and Path(p.repo_path).is_dir()
        ]
        # Include Personal project so it appears in filter dropdowns
        if not any(p["id"] == PERSONAL_PROJECT_ID for p in result):
            result.insert(0, {"id": PERSONAL_PROJECT_ID, "name": "Personal", "repo_path": None})
        return result

    @router.get("/tree")
    async def list_directory(
        project_id: str = Query(..., description="Project ID"),
        path: str = Query("", description="Relative path within project"),
    ) -> list[dict[str, Any]]:
        """List directory contents for the file tree.

        Returns entries sorted: directories first, then files, both alphabetical.
        Respects .gitignore via git ls-files.
        """
        pm = _get_project_manager(server)
        project = pm.get(project_id)
        if not project or not project.repo_path:
            raise HTTPException(404, f"Project not found: {project_id}")

        repo_path: str = project.repo_path
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
        max_size: int = Query(DEFAULT_MAX_SIZE, description="Max bytes to read"),
    ) -> dict[str, Any]:
        """Read file content.

        Returns content with metadata. Large files are truncated.
        Binary files return metadata only.
        """
        pm = _get_project_manager(server)
        project = pm.get(project_id)
        if not project or not project.repo_path:
            raise HTTPException(404, "Project not found")

        target = _resolve_safe_path(project.repo_path, path)
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

        # Read text content
        # Read text content (async)
        try:
            loop = asyncio.get_running_loop()
            raw = await loop.run_in_executor(None, target.read_bytes)
            truncated = len(raw) > max_size
            if truncated:
                raw = raw[:max_size]
            content = raw.decode("utf-8", errors="replace")
            result["content"] = content
            result["truncated"] = truncated
        except OSError as e:
            logger.error(f"Failed to read file: {e} (project_id={project_id})")
            raise HTTPException(500, f"Failed to read file: {e}") from e

        return result

    @router.get("/image")
    async def serve_image(
        project_id: str = Query(..., description="Project ID"),
        path: str = Query(..., description="Relative path within project"),
    ) -> FileResponse:
        """Serve an image file directly for <img> tags."""
        pm = _get_project_manager(server)
        project = pm.get(project_id)
        if not project or not project.repo_path:
            raise HTTPException(404, "Project not found")

        target = _resolve_safe_path(project.repo_path, path)
        if not target.is_file():
            raise HTTPException(404, "File not found")

        extension = target.suffix.lower()
        if extension not in IMAGE_EXTENSIONS:
            raise HTTPException(400, "Not an image file")

        mime_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return FileResponse(target, media_type=mime_type)

    class WriteFileRequest(BaseModel):
        project_id: str
        path: str
        content: str

    @router.post("/write")
    async def write_file(request: WriteFileRequest) -> dict[str, Any]:
        """Write content to a file.

        Refuses writes to .git/ directory.
        """
        pm = _get_project_manager(server)
        project = pm.get(request.project_id)
        if not project or not project.repo_path:
            raise HTTPException(404, "Project not found")

        target = _resolve_safe_path(project.repo_path, request.path)

        # Refuse writes to .git/
        rel = str(target.relative_to(Path(project.repo_path).resolve()))
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
        pm = _get_project_manager(server)
        project = pm.get(project_id)
        if not project or not project.repo_path:
            raise HTTPException(404, "Project not found")

        result: dict[str, Any] = {"branch": None, "files": {}}

        try:
            # Get branch name
            rc_branch, stdout_branch = await _run_git(
                project.repo_path, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5
            )
            if rc_branch == 0:
                result["branch"] = stdout_branch.strip()

            # Get file statuses
            rc_status, stdout_status = await _run_git(
                project.repo_path, ["status", "--porcelain"], timeout=10
            )

            if rc_status == 0:
                files: dict[str, str] = {}
                for line in stdout_status.splitlines():
                    if not line or len(line) < GIT_PORCELAIN_STATUS_MIN_LINE_LENGTH:
                        continue
                    # Format: "XY PATH" — XY is exactly 2 chars, then space, then path
                    xy = line[0:2]
                    status_code = xy.strip() or "?"
                    file_path = line[GIT_PORCELAIN_PATH_OFFSET:]
                    # Handle renames: "R  old -> new"
                    if " -> " in file_path:
                        file_path = file_path.split(" -> ")[-1]
                    if file_path:
                        files[file_path] = status_code
                result["files"] = files
        except Exception:
            logger.debug("Failed to get git status", exc_info=True)

        return result

    @router.get("/git-diff")
    async def git_diff(
        project_id: str = Query(..., description="Project ID"),
        path: str = Query(..., description="Relative file path"),
    ) -> dict[str, str]:
        """Get git diff for a specific file."""
        pm = _get_project_manager(server)
        project = pm.get(project_id)
        if not project or not project.repo_path:
            raise HTTPException(404, "Project not found")

        # Validate path
        _resolve_safe_path(project.repo_path, path)

        try:
            rc, stdout = await _run_git(project.repo_path, ["diff", "HEAD", "--", path], timeout=10)
            diff = stdout if rc == 0 else ""
        except Exception:
            diff = ""

        return {"diff": diff, "path": path}

    return router
