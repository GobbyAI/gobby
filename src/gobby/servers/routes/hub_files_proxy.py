"""Hub-owner USER.md surfaces. Checkout browsing stays in files.py."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from gobby.files_home_http import (
    FILES_PROXY_HOP_HEADER,
    USER_MD_CONTENT_MAX_BYTES,
    USER_MD_PATH,
    USER_MD_WIRE_MAX_BYTES,
    hop_header_present,
    is_remote_files_mode,
)
from gobby.hooks.event_handlers._session_start.profile import (
    USER_PROFILE_FILENAME,
    UserProfileError,
    read_user_profile_content,
    write_user_profile_content,
)
from gobby.paths import FilesHomeError, FilesHomeNotOnThisDaemonError, require_files_home


def create_hub_files_proxy_router() -> APIRouter:
    router = APIRouter(tags=["files"])

    @router.get(USER_MD_PATH)
    async def get_user_md() -> dict[str, str]:
        _refuse_remote_target()
        try:
            require_files_home()
            return {"content": read_user_profile_content()}
        except FilesHomeNotOnThisDaemonError as exc:
            raise _remote_target() from exc
        except FilesHomeError as exc:
            raise HTTPException(status_code=404, detail="files_home is missing") from exc

    @router.put(USER_MD_PATH)
    async def put_user_md(request: Request) -> dict[str, str]:
        _refuse_remote_target()
        content = await _read_user_md_content(request)
        try:
            write_user_profile_content(content)
        except UserProfileError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except FilesHomeNotOnThisDaemonError as exc:
            raise _remote_target() from exc
        except FilesHomeError as exc:
            raise HTTPException(status_code=409, detail="files_home is missing") from exc
        return {"content": content}

    return router


def _refuse_remote_target() -> None:
    if is_remote_files_mode():
        raise _remote_target()


def _remote_target() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"error": "remote_target", "message": "this daemon is not the files owner"},
    )


async def _read_user_md_content(request: Request) -> str:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        if length > USER_MD_WIRE_MAX_BYTES:
            raise HTTPException(status_code=413, detail="USER.md body exceeds wire limit")

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > USER_MD_WIRE_MAX_BYTES:
            raise HTTPException(status_code=413, detail="USER.md body exceeds wire limit")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="USER.md body must be JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
        raise HTTPException(status_code=400, detail="body must be {content: string}")
    content = payload["content"]
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="body must be {content: string}")
    if len(content.encode("utf-8")) > USER_MD_CONTENT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="USER.md content exceeds decoded limit")
    return content


def hop_refused_response() -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"error": "hop_refused", "message": "X-Gobby-Files-Proxy-Hop already present"},
        headers={FILES_PROXY_HOP_HEADER: "1"},
    )


def hop_already_present(request: Request) -> bool:
    return hop_header_present(request.headers)


__all__ = [
    "USER_PROFILE_FILENAME",
    "create_hub_files_proxy_router",
    "hop_already_present",
    "hop_refused_response",
]
