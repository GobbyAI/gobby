"""HTTP routes for stored chat attachments."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import aiofiles
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import gobby.storage.chat_attachments as chat_attachments
from gobby.paths import get_gobby_home
from gobby.servers.chat_attachment_limits import resolve_chat_attachment_limits
from gobby.storage.config_store import ConfigStore
from gobby.storage.database import DatabaseProtocol
from gobby.storage.projects import PERSONAL_PROJECT_ID, LocalProjectManager

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

_UPLOAD_CHUNK_BYTES = 1024 * 1024
_GENERIC_BINARY_MIME_TYPES = {"application/octet-stream", "binary/octet-stream"}
_TEXT_COMPATIBLE_MIME_TYPES = {
    "application/json",
    "application/javascript",
    "application/typescript",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
}
_ZIP_CONTAINER_MIME_TYPES = {
    "application/epub+zip",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _safe_path_part(value: str, fallback: str) -> str:
    cleaned = value.replace("\x00", "").replace("/", "_").replace("\\", "_")
    cleaned = cleaned.lstrip(".")
    cleaned = re.sub(r"[^\w.\-]", "_", cleaned)
    return cleaned or fallback


def _attachment_dir(project_id: str, attachment_id: str) -> Path:
    return (
        get_gobby_home()
        / "projects"
        / project_id
        / "attachments"
        / attachment_id[:2]
        / attachment_id
    )


def _content_disposition(mime_type: str) -> str:
    if mime_type.startswith("image/") or mime_type == "application/pdf":
        return "inline"
    return "attachment"


def _declared_mime_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower() or "application/octet-stream"


def _sniff_mime_type(sample: bytes) -> str | None:
    if not sample:
        return None
    if sample.startswith(b"%PDF-"):
        return "application/pdf"
    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if sample.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if sample.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(sample) >= 12 and sample[:4] == b"RIFF" and sample[8:12] == b"WEBP":
        return "image/webp"
    if sample.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "application/zip"
    if b"\x00" not in sample:
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return "text/plain"
    return None


def _mime_matches_declared(declared: str, sniffed: str | None) -> bool:
    if sniffed is None or declared in _GENERIC_BINARY_MIME_TYPES:
        return True
    if declared == sniffed:
        return True
    if sniffed == "text/plain" and (
        declared.startswith("text/") or declared in _TEXT_COMPATIBLE_MIME_TYPES
    ):
        return True
    if sniffed == "application/zip" and declared in _ZIP_CONTAINER_MIME_TYPES:
        return True
    return False


async def _validate_declared_mime(path: Path, declared: str) -> None:
    def read_sample() -> bytes:
        with path.open("rb") as handle:
            return handle.read(512)

    sniffed = _sniff_mime_type(await asyncio.to_thread(read_sample))
    if _mime_matches_declared(declared, sniffed):
        return
    raise HTTPException(
        status_code=415,
        detail=f"Uploaded file content appears to be {sniffed}, not {declared}",
    )


async def _remove_path(path: Path) -> bool:
    try:
        await asyncio.to_thread(path.unlink)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        logger.warning("Failed to remove chat attachment path %s", path, exc_info=True)
        return False


def _get_config_store(server: HTTPServer) -> ConfigStore:
    existing = getattr(server.services, "config_store", None)
    if existing is not None:
        return cast(ConfigStore, existing)
    return ConfigStore(server.services.database)


def _resolve_upload_project_id(
    db: DatabaseProtocol,
    requested_project_id: str | None,
    service_project_id: str | None,
) -> str:
    project_manager = LocalProjectManager(db)
    if requested_project_id:
        project = project_manager.get(requested_project_id)
        if project is None or project.deleted_at is not None:
            raise ValueError("Unknown project_id")
        return requested_project_id

    if service_project_id:
        project = project_manager.get(service_project_id)
        if project is not None and project.deleted_at is None:
            return service_project_id

    return PERSONAL_PROJECT_ID


def create_chat_attachments_router(server: HTTPServer) -> APIRouter:
    """Create routes for chat attachment upload and retrieval."""
    router = APIRouter(prefix="/api/chat/attachments", tags=["chat"])

    @router.post("")
    async def upload_attachment(
        file: UploadFile = File(...),
        draft_id: str | None = Form(default=None),
        project_id: str | None = Form(default=None),
    ) -> dict[str, Any]:
        config_store = _get_config_store(server)
        limits = resolve_chat_attachment_limits(
            config_store=config_store,
            daemon_config=server.config,
        )
        try:
            resolved_project_id = await server.run_db(
                _resolve_upload_project_id,
                server.services.database,
                project_id,
                server.services.project_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        attachment_id = str(uuid4())
        filename = Path(file.filename or "attachment").name or "attachment"
        safe_name = _safe_path_part(filename, "attachment")
        mime_type = _declared_mime_type(
            file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )
        target_dir = _attachment_dir(resolved_project_id, attachment_id)
        target_path = target_dir / safe_name
        temp_path = target_dir / f".{safe_name}.part"
        size = 0

        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        try:
            async with aiofiles.open(temp_path, "wb") as out:
                while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                    size += len(chunk)
                    if size > limits.max_file_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"Attachment exceeds configured {limits.max_file_bytes} byte limit"
                            ),
                        )
                    await out.write(chunk)

            await _validate_declared_mime(temp_path, mime_type)
            await asyncio.to_thread(temp_path.replace, target_path)
            record = await server.run_db(
                chat_attachments.create_attachment,
                server.services.database,
                attachment_id=attachment_id,
                project_id=resolved_project_id,
                draft_id=draft_id,
                filename=filename,
                mime_type=mime_type,
                size_bytes=size,
                local_path=str(target_path),
            )
        except Exception:
            await _remove_path(temp_path)
            await _remove_path(target_path)
            raise

        return chat_attachments.to_api_dict(record)

    @router.get("/{attachment_id}/content")
    async def get_attachment_content(attachment_id: str) -> FileResponse:
        record = await server.run_db(
            chat_attachments.get_attachment,
            server.services.database,
            attachment_id,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="Attachment not found")

        path = Path(record.local_path)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Attachment content not found")

        return FileResponse(
            path,
            media_type=record.mime_type,
            filename=record.filename,
            content_disposition_type=_content_disposition(record.mime_type),
        )

    @router.delete("/{attachment_id}")
    async def delete_attachment(attachment_id: str) -> dict[str, bool]:
        try:
            record = await server.run_db(
                chat_attachments.delete_unbound_attachment,
                server.services.database,
                attachment_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="Attachment not found")

        removed = await _remove_path(Path(record.local_path))
        return {"ok": removed}

    return router
