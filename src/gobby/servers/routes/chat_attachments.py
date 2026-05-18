"""HTTP routes for stored chat attachments."""

from __future__ import annotations

import asyncio
import codecs
import logging
import mimetypes
import re
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

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
_SAFE_PATH_PART_MAX_BYTES = 255


def _safe_path_part(value: str, fallback: str) -> str:
    cleaned = value.replace("\x00", "").replace("/", "_").replace("\\", "_")
    cleaned = cleaned.lstrip(".")
    cleaned = re.sub(r"[^A-Za-z0-9.\-]", "_", cleaned)
    return _truncate_path_part_utf8(cleaned or fallback, fallback)


def _truncate_path_part_utf8(value: str, fallback: str) -> str:
    if len(value.encode("utf-8")) <= _SAFE_PATH_PART_MAX_BYTES:
        return value

    def utf8_prefix(text: str, max_bytes: int) -> str:
        if max_bytes <= 0:
            return ""
        encoded = text.encode("utf-8")[:max_bytes]
        return encoded.decode("utf-8", errors="ignore")

    fallback_name = utf8_prefix(fallback, _SAFE_PATH_PART_MAX_BYTES) or "attachment"
    suffix = Path(value).suffix
    stem = value[: -len(suffix)] if suffix else value
    if suffix:
        suffix = utf8_prefix(suffix, _SAFE_PATH_PART_MAX_BYTES - 1)
    suffix_bytes = suffix.encode("utf-8")
    stem_budget = _SAFE_PATH_PART_MAX_BYTES - len(suffix_bytes)
    stem = utf8_prefix(stem, stem_budget).rstrip("._-")
    candidate = f"{stem}{suffix}" if stem else fallback_name
    if len(candidate.encode("utf-8")) > _SAFE_PATH_PART_MAX_BYTES:
        candidate = utf8_prefix(candidate, _SAFE_PATH_PART_MAX_BYTES)
    return candidate or fallback_name


def _temp_upload_name(safe_name: str) -> str:
    temp_name = f".{safe_name}.part"
    if len(temp_name.encode("utf-8")) <= _SAFE_PATH_PART_MAX_BYTES:
        return temp_name
    budget = _SAFE_PATH_PART_MAX_BYTES - len("..part")
    encoded = safe_name.encode("utf-8")[:budget]
    stem = encoded.decode("utf-8", errors="ignore").rstrip("._-") or "attachment"
    return f".{stem}.part"


def _attachment_dir(project_id: str, attachment_id: str) -> Path:
    safe_project_id = _safe_path_part(project_id, "project")
    return (
        get_gobby_home()
        / "projects"
        / safe_project_id
        / "attachments"
        / attachment_id[:2]
        / attachment_id
    )


def _validate_uuid_param(value: str, name: str) -> None:
    try:
        UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Invalid {name}") from exc


def _ensure_disk_space(directory: Path, incoming_bytes: int) -> None:
    try:
        usage = shutil.disk_usage(directory)
    except OSError as exc:
        raise HTTPException(status_code=507, detail="Attachment storage unavailable") from exc
    if usage.free < incoming_bytes:
        raise HTTPException(status_code=507, detail="Insufficient disk space for attachment")


def _content_disposition(mime_type: str) -> str:
    if mime_type.startswith("image/") or mime_type == "application/pdf":
        return "inline"
    return "attachment"


def _declared_mime_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower() or "application/octet-stream"


def resolve_mime_type(content_type: str | None, filename: str) -> str:
    """Resolve the normalized MIME type for an upload."""
    guessed_type = mimetypes.guess_type(filename)[0]
    return _declared_mime_type(content_type or guessed_type or "application/octet-stream")


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


def _requires_utf8_validation(declared: str, sniffed: str | None) -> bool:
    return (
        sniffed == "text/plain"
        or declared.startswith("text/")
        or declared in _TEXT_COMPATIBLE_MIME_TYPES
    )


def _validate_utf8_file_sync(path: Path) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")()
    with path.open("rb") as handle:
        while chunk := handle.read(_UPLOAD_CHUNK_BYTES):
            decoder.decode(chunk, final=False)
    decoder.decode(b"", final=True)


async def _validate_declared_mime(
    path: Path,
    declared: str,
    *,
    filename: str = "attachment",
    attachment_id: str = "unknown",
) -> None:
    def read_sample() -> bytes:
        with path.open("rb") as handle:
            return handle.read(512)

    sniffed = _sniff_mime_type(await asyncio.to_thread(read_sample))
    if _requires_utf8_validation(declared, sniffed):
        try:
            await asyncio.to_thread(_validate_utf8_file_sync, path)
        except UnicodeDecodeError as exc:
            logger.warning(
                "Uploaded text attachment failed UTF-8 validation: filename=%s attachment_id=%s",
                filename,
                attachment_id,
                exc_info=True,
            )
            raise HTTPException(
                status_code=415,
                detail=f"Uploaded file {filename!r} is not valid UTF-8 text",
            ) from exc
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
    except OSError:
        logger.warning("Failed to remove chat attachment path %s", path, exc_info=True)
        return False


async def _remove_empty_directory(path: Path) -> None:
    try:
        await asyncio.to_thread(path.rmdir)
    except FileNotFoundError:
        return
    except OSError:
        return


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
        raw_filename = (file.filename or "attachment").replace("\x00", "") or "attachment"
        safe_name = _safe_path_part(raw_filename, "attachment")
        filename = safe_name
        mime_type = resolve_mime_type(file.content_type, raw_filename)
        target_dir = _attachment_dir(resolved_project_id, attachment_id)
        target_path = target_dir / safe_name
        temp_path = target_dir / _temp_upload_name(safe_name)
        size = 0
        replace_completed = False

        try:
            await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
            known_file_size = getattr(file, "size", None)
            reserved_bytes = (
                min(limits.max_file_bytes, known_file_size)
                if isinstance(known_file_size, int) and known_file_size >= 0
                else limits.max_file_bytes
            )
            await asyncio.to_thread(_ensure_disk_space, target_dir, reserved_bytes)
            async with aiofiles.open(temp_path, "wb") as out:
                while True:
                    remaining = limits.max_file_bytes - size
                    # Read one byte past the budget so oversized uploads are rejected immediately.
                    read_size = min(_UPLOAD_CHUNK_BYTES, max(remaining + 1, 1))
                    chunk = await file.read(read_size)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limits.max_file_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"Attachment exceeds configured {limits.max_file_bytes} byte limit"
                            ),
                        )
                    await out.write(chunk)

            await _validate_declared_mime(
                temp_path,
                mime_type,
                filename=filename,
                attachment_id=attachment_id,
            )
            await asyncio.to_thread(temp_path.replace, target_path)
            replace_completed = True
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
        except HTTPException:
            await _remove_path(temp_path)
            if replace_completed:
                await _remove_path(target_path)
            await _remove_empty_directory(target_dir)
            raise
        except OSError as exc:
            logger.warning("Attachment upload storage operation failed", exc_info=True)
            await _remove_path(temp_path)
            if replace_completed:
                await _remove_path(target_path)
            await _remove_empty_directory(target_dir)
            raise HTTPException(status_code=507, detail="Attachment storage unavailable") from exc
        except Exception:
            await _remove_path(temp_path)
            if replace_completed:
                await _remove_path(target_path)
            await _remove_empty_directory(target_dir)
            raise

        return chat_attachments.to_api_dict(record)

    @router.get("/{attachment_id}/content")
    async def get_attachment_content(attachment_id: str) -> FileResponse:
        _validate_uuid_param(attachment_id, "attachment_id")
        record = await server.run_db(
            chat_attachments.get_attachment,
            server.services.database,
            attachment_id,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="Attachment not found")

        path = Path(record.local_path)
        try:
            stat_result = await asyncio.to_thread(path.stat)
        except OSError as exc:
            raise HTTPException(status_code=404, detail="Attachment content not found") from exc
        if not stat.S_ISREG(stat_result.st_mode):
            raise HTTPException(status_code=404, detail="Attachment content not found")

        return FileResponse(
            path,
            media_type=record.mime_type,
            filename=record.filename,
            content_disposition_type=_content_disposition(record.mime_type),
            stat_result=stat_result,
        )

    @router.delete("/{attachment_id}")
    async def delete_attachment(attachment_id: str) -> dict[str, bool]:
        _validate_uuid_param(attachment_id, "attachment_id")
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
