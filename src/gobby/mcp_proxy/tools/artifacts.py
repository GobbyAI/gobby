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
        description="Show a file in the web chat artifacts panel with syntax highlighting (code) or rendered markdown (text). Supports code, markdown, images, and CSV files.",
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
        project_path = project_ctx.get("project_path") if project_ctx else None
        if not isinstance(project_path, str) or not project_path:
            return {"success": False, "error": "project context is required"}
        project_root = Path(project_path).resolve(strict=False)
        resolved_source = source.resolve(strict=False)
        if resolved_source != project_root and project_root not in resolved_source.parents:
            return {"success": False, "error": f"file_path must be under project: {file_path}"}
        if not source.is_file():
            return {"success": False, "error": f"File not found: {file_path}"}

        ext_clean = source.suffix.lower().lstrip(".")
        artifact_type, language = EXTENSION_MAP.get(
            f".{ext_clean}" if ext_clean else "",
            ("code", ext_clean or "text"),
        )

        # Check file size
        file_size = source.stat().st_size
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
            raw = await asyncio.to_thread(source.read_bytes)
            mime_type = mimetypes.guess_type(str(source))[0] or "application/octet-stream"
            content = f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"
        else:
            try:
                content = await asyncio.to_thread(source.read_text, encoding="utf-8")
            except UnicodeDecodeError:
                return {"success": False, "error": f"File is not valid UTF-8: {file_path}"}

        actual_title = title or source.name

        bc = _artifact_broadcaster
        if bc:
            await bc(
                event="show_file",
                conversation_id=actual_convo_id,
                artifact_type=artifact_type,
                content=content,
                language=language,
                title=actual_title,
            )

        return {
            "success": True,
            "type": artifact_type,
            "language": language,
            "title": actual_title,
        }

    return registry
