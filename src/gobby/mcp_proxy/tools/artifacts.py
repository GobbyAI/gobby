"""Internal MCP tools for the Gobby artifacts system.

The ``gobby-artifacts`` server broadcasts file content into the web chat
artifacts panel. ``show_file`` reads a file from disk, classifies it by
extension into an artifact type, and pushes it to the panel through the
artifact broadcaster wired in the HTTP lifespan.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.utils.project_context import get_project_context

logger = logging.getLogger(__name__)

ArtifactBroadcaster = Callable[..., Awaitable[None]]

# Artifact broadcaster, wired after creation in the HTTP lifespan.
_artifact_broadcaster: ArtifactBroadcaster | None = None

MAX_TEXT_FILE_SIZE = 1 * 1024 * 1024  # 1MB
MAX_IMAGE_FILE_SIZE = 5 * 1024 * 1024  # 5MB
_PROJECT_ROOT_KEYS = ("project_path", "parent_project_path")
_EXPLICIT_ROOT_KEYS = (
    "artifact_allowed_roots",
    "artifact_roots",
    "allowed_artifact_roots",
)

# Extension → (artifact_type, language)
EXTENSION_MAP: dict[str, tuple[str, str | None]] = {
    ".md": ("text", "markdown"),
    ".txt": ("text", "plaintext"),
    ".rst": ("text", "plaintext"),
    ".adoc": ("text", "plaintext"),
    ".py": ("code", "python"),
    ".js": ("code", "javascript"),
    ".ts": ("code", "typescript"),
    ".tsx": ("code", "tsx"),
    ".jsx": ("code", "jsx"),
    ".rs": ("code", "rust"),
    ".go": ("code", "go"),
    ".java": ("code", "java"),
    ".json": ("code", "json"),
    ".yaml": ("code", "yaml"),
    ".yml": ("code", "yaml"),
    ".toml": ("code", "toml"),
    ".html": ("code", "html"),
    ".css": ("code", "css"),
    ".sql": ("code", "sql"),
    ".sh": ("code", "shell"),
    ".bash": ("code", "shell"),
    ".zsh": ("code", "shell"),
    ".c": ("code", "c"),
    ".cpp": ("code", "cpp"),
    ".h": ("code", "c"),
    ".rb": ("code", "ruby"),
    ".php": ("code", "php"),
    ".swift": ("code", "swift"),
    ".kt": ("code", "kotlin"),
    ".scala": ("code", "scala"),
    ".r": ("code", "r"),
    ".lua": ("code", "lua"),
    ".xml": ("code", "xml"),
    ".csv": ("sheet", None),
    ".tsv": ("sheet", None),
    ".png": ("image", None),
    ".jpg": ("image", None),
    ".jpeg": ("image", None),
    ".gif": ("image", None),
    ".webp": ("image", None),
    ".svg": ("image", None),
}


def set_artifact_broadcaster(callback: ArtifactBroadcaster | None) -> None:
    """Set the artifact broadcaster after creation (wired in HTTP lifespan)."""
    global _artifact_broadcaster
    _artifact_broadcaster = callback


def create_artifacts_registry() -> InternalToolRegistry:
    registry = InternalToolRegistry(
        name="gobby-artifacts",
        description="Artifact display - show_file",
    )

    @registry.tool(
        name="show_file",
        description=(
            "Show a file in the web chat artifacts panel with syntax highlighting "
            "(code) or rendered markdown (text). Supports code, markdown, images, "
            "and CSV files."
        ),
    )
    async def show_file(
        file_path: str,
        title: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Show a file in the artifacts panel."""
        from gobby.utils.session_context import get_session_context

        actual_convo_id = conversation_id
        if not actual_convo_id:
            ctx = get_session_context()
            if ctx:
                actual_convo_id = ctx.conversation_id or ctx.session_id

        if not actual_convo_id:
            return {"success": False, "error": "conversation_id (or session context) is required"}

        source = Path(file_path)
        if not source.is_absolute():
            return {"success": False, "error": f"file_path must be absolute: {file_path}"}
        project_ctx = get_project_context()
        if project_ctx is None:
            return {"success": False, "error": "project context missing"}
        project_path = project_ctx.get("project_path")
        if not isinstance(project_path, str) or not project_path:
            return {
                "success": False,
                "error": "project_path is required and must be a non-empty string",
            }
        try:
            resolved_source = source.resolve(strict=True)
        except FileNotFoundError:
            return {"success": False, "error": f"File not found: {file_path}"}
        except OSError as exc:
            logger.debug(
                "Failed to resolve artifact source path",
                extra={"file_path": file_path, "error": str(exc)},
                exc_info=True,
            )
            return {"success": False, "error": f"File not found: {file_path}"}

        allowed_roots = _artifact_allowed_roots(project_ctx)
        allowed_root_values = [str(root) for root in allowed_roots]
        if not any(_is_relative_to(resolved_source, root) for root in allowed_roots):
            return {
                "success": False,
                "error": (
                    "file_path must be under one of the allowed project roots: "
                    f"{file_path}; allowed_roots={allowed_root_values}"
                ),
            }
        if not resolved_source.is_file():
            return {
                "success": False,
                "error": f"File not found: {file_path}; allowed_roots={allowed_root_values}",
            }

        ext_clean = resolved_source.suffix.lower().lstrip(".")
        artifact_type, language = EXTENSION_MAP.get(
            f".{ext_clean}" if ext_clean else "",
            ("code", ext_clean or "text"),
        )

        # Check file size
        file_size = resolved_source.stat().st_size
        if artifact_type == "image":
            if file_size > MAX_IMAGE_FILE_SIZE:
                return {
                    "success": False,
                    "error": f"Image file too large: {file_size} bytes (max {MAX_IMAGE_FILE_SIZE})",
                }
        elif file_size > MAX_TEXT_FILE_SIZE:
            return {
                "success": False,
                "error": f"File too large: {file_size} bytes (max {MAX_TEXT_FILE_SIZE})",
            }

        # Read content (use to_thread to avoid blocking the event loop)
        if artifact_type == "image":
            raw = await asyncio.to_thread(resolved_source.read_bytes)
            mime_type = mimetypes.guess_type(str(resolved_source))[0] or "application/octet-stream"
            content = f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"
        else:
            try:
                content = await asyncio.to_thread(resolved_source.read_text, encoding="utf-8")
            except UnicodeDecodeError:
                return {"success": False, "error": f"File is not valid UTF-8: {file_path}"}

        actual_title = title or resolved_source.name

        bc = _artifact_broadcaster
        broadcast = False
        if bc is not None:
            await bc(
                event="show_file",
                conversation_id=actual_convo_id,
                artifact_type=artifact_type,
                content=content,
                language=language,
                title=actual_title,
            )
            broadcast = True

        return {
            "success": True,
            "broadcast": broadcast,
            "type": artifact_type,
            "language": language,
            "title": actual_title,
        }

    return registry


def _artifact_allowed_roots(project_ctx: dict[str, Any]) -> list[Path]:
    roots: list[Path] = []
    for key in _PROJECT_ROOT_KEYS:
        _append_context_root(roots, project_ctx.get(key))
    for key in _EXPLICIT_ROOT_KEYS:
        value = project_ctx.get(key)
        if isinstance(value, str):
            _append_context_root(roots, value)
        elif isinstance(value, list):
            for item in value:
                _append_context_root(roots, item)
    return list(dict.fromkeys(roots))


def _append_context_root(roots: list[Path], value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        return
    try:
        roots.append(Path(value).expanduser().resolve(strict=True))
    except OSError:
        logger.debug("Skipping unavailable artifact root", extra={"root": value}, exc_info=True)


def _is_relative_to(path: Path, root: Path) -> bool:
    return path == root or root in path.parents
