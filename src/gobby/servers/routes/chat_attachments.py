"""HTTP routes for stored chat attachments."""

from __future__ import annotations

import asyncio
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

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

_UPLOAD_CHUNK_BYTES = 1024 * 1024


def _safe_path_part(value: str, fallback: str) -> str:
    cleaned = value.replace("\x00", "").replace("/", "_").replace("\\", "_")
    cleaned = cleaned.lstrip(".")
    cleaned = re.sub(r"[^\w.\-]", "_", cleaned)
    return cleaned or fallback


def _attachment_dir(attachment_id: str) -> Path:
    return get_gobby_home() / "chat_attachments" / attachment_id[:2] / attachment_id


def _content_disposition(mime_type: str) -> str:
    if mime_type.startswith("image/") or mime_type == "application/pdf":
        return "inline"
    return "attachment"


async def _remove_path(path: Path) -> None:
    try:
        await asyncio.to_thread(path.unlink)
    except FileNotFoundError:
        pass


def _get_config_store(server: HTTPServer) -> ConfigStore:
    existing = getattr(server.services, "config_store", None)
    if existing is not None:
        return cast(ConfigStore, existing)
    return ConfigStore(server.services.database)


def create_chat_attachments_router(server: HTTPServer) -> APIRouter:
    """Create routes for chat attachment upload and retrieval."""
    router = APIRouter(prefix="/api/chat/attachments", tags=["chat"])

    @router.post("")
    async def upload_attachment(
        file: UploadFile = File(...),
        draft_id: str | None = Form(default=None),
    ) -> dict[str, Any]:
        config_store = _get_config_store(server)
        limits = resolve_chat_attachment_limits(
            config_store=config_store,
            daemon_config=server.config,
        )

        attachment_id = str(uuid4())
        filename = Path(file.filename or "attachment").name or "attachment"
        safe_name = _safe_path_part(filename, "attachment")
        mime_type = (
            file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )
        target_dir = _attachment_dir(attachment_id)
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

            await asyncio.to_thread(temp_path.replace, target_path)
            record = await server.run_db(
                chat_attachments.create_attachment,
                server.services.database,
                attachment_id=attachment_id,
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
        if not path.exists():
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

        await _remove_path(Path(record.local_path))
        return {"ok": True}

    return router
