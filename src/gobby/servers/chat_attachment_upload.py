"""Owner upload publication for stored chat attachments."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile

import gobby.storage.chat_attachments as chat_attachments
from gobby.paths import FilesHomeError, FilesHomeNotOnThisDaemonError, require_files_home
from gobby.servers.chat_attachment_files import (
    attachment_relative_locator,
    attachment_temp_locator,
    unlink_attachment_bytes,
)
from gobby.servers.chat_attachment_workers import run_shielded
from gobby.storage.chat_attachment_lease import mark_published_db, new_claim_token
from gobby.utils.durable_file import durable_replace_files_home

_UPLOAD_CHUNK_BYTES = 1024 * 1024
RunDb = Callable[..., Awaitable[Any]]


async def publish_uploaded_attachment(
    *,
    file: UploadFile,
    resolved_project_id: str,
    attachment_id: str,
    filename: str,
    mime_type: str,
    draft_id: str | None,
    max_file_bytes: int,
    database: Any,
    run_db: RunDb,
    validate_mime: Callable[..., Awaitable[None]],
    ensure_disk_space: Callable[[Path, int], None],
) -> dict[str, Any]:
    """Stream, persist unpublished metadata, then publish through a shielded worker."""
    try:
        require_files_home()
    except FilesHomeNotOnThisDaemonError as exc:
        raise HTTPException(status_code=409, detail="files_home is not on this daemon") from exc
    except FilesHomeError as exc:
        raise HTTPException(status_code=507, detail="Attachment storage unavailable") from exc

    locator = attachment_relative_locator(resolved_project_id, attachment_id, filename)
    temp_locator = attachment_temp_locator(resolved_project_id, attachment_id, filename)
    claim_token = new_claim_token()
    known_file_size = getattr(file, "size", None)
    reserved_bytes = (
        min(max_file_bytes, known_file_size)
        if isinstance(known_file_size, int) and known_file_size >= 0
        else max_file_bytes
    )
    staging = tempfile.NamedTemporaryFile(delete=False)
    staging_path = Path(staging.name)
    size = 0
    row_created = False
    try:
        await asyncio.to_thread(ensure_disk_space, staging_path.parent, reserved_bytes)
        while True:
            remaining = max_file_bytes - size
            read_size = min(_UPLOAD_CHUNK_BYTES, max(remaining + 1, 1))
            chunk = await file.read(read_size)
            if not chunk:
                break
            size += len(chunk)
            if size > max_file_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Attachment exceeds configured {max_file_bytes} byte limit",
                )
            staging.write(chunk)
        staging.close()
        await validate_mime(
            staging_path,
            mime_type,
            filename=filename,
            attachment_id=attachment_id,
        )
        record = await run_db(
            chat_attachments.create_attachment,
            database,
            attachment_id=attachment_id,
            project_id=resolved_project_id,
            draft_id=draft_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size,
            local_path=locator,
            published=False,
            claim_token=claim_token,
        )
        row_created = True
        publish_outcome, publish_cancelled = await run_shielded(
            "attachment-publish",
            durable_replace_files_home,
            staging_path,
            locator,
            temp_locator,
        )
        if publish_outcome.error is not None:
            await _compensate_unpublished(
                run_db,
                database,
                resolved_project_id,
                attachment_id,
                filename,
                row_created=row_created,
            )
            if publish_cancelled:
                raise asyncio.CancelledError
            raise HTTPException(
                status_code=507, detail="Attachment storage unavailable"
            ) from publish_outcome.error
        cas_outcome, cas_cancelled = await run_shielded(
            "attachment-publish-cas",
            mark_published_db,
            database,
            attachment_id=attachment_id,
            project_id=resolved_project_id,
            token=claim_token,
        )
        published_record = await run_db(
            chat_attachments.get_attachment,
            database,
            attachment_id,
            require_published=False,
        )
        if cas_outcome.error is None and cas_outcome.result:
            if cas_cancelled:
                raise asyncio.CancelledError
            return chat_attachments.to_api_dict(record)
        if published_record is not None and published_record.published:
            if cas_cancelled:
                raise asyncio.CancelledError
            return chat_attachments.to_api_dict(published_record)
        if published_record is None:
            await asyncio.to_thread(
                unlink_attachment_bytes, resolved_project_id, attachment_id, filename
            )
        if cas_cancelled:
            raise asyncio.CancelledError
        raise HTTPException(status_code=507, detail="Attachment storage unavailable")
    except HTTPException:
        if row_created:
            await asyncio.to_thread(
                unlink_attachment_bytes, resolved_project_id, attachment_id, filename
            )
        raise
    except FilesHomeError as exc:
        if row_created:
            await _compensate_unpublished(
                run_db,
                database,
                resolved_project_id,
                attachment_id,
                filename,
                row_created=True,
            )
        raise HTTPException(status_code=507, detail="Attachment storage unavailable") from exc
    finally:
        staging.close()
        staging_path.unlink(missing_ok=True)


async def _compensate_unpublished(
    run_db: RunDb,
    database: Any,
    project_id: str,
    attachment_id: str,
    filename: str,
    *,
    row_created: bool,
) -> None:
    await asyncio.to_thread(unlink_attachment_bytes, project_id, attachment_id, filename)
    if row_created:
        await run_db(
            chat_attachments.delete_attachment_row,
            database,
            attachment_id=attachment_id,
            project_id=project_id,
        )
