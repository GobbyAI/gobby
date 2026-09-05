"""Forward file-bearing HTTP requests to the hub that owns their storage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from gobby.files_home_http import (
    FORWARD_REQUEST_HEADERS,
    FORWARD_RESPONSE_HEADERS,
    PROXY_ACCEPT_STATUSES,
    hop_header_present,
    is_remote_files_mode,
    require_hub_daemon_url,
)
from gobby.utils.daemon_client import DaemonAuthenticationError, DaemonClient, DaemonClientError


def as_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise HTTPException(status_code=502, detail="hub returned a non-object JSON body")


async def proxy_owner_request(
    request: Request,
    *,
    stream_body: bool = False,
    accept_statuses: tuple[int, ...] = PROXY_ACCEPT_STATUSES,
    json_body: Mapping[str, Any] | None = None,
) -> Any:
    if hop_header_present(request.headers) and is_remote_files_mode():
        raise HTTPException(
            status_code=409,
            detail={"error": "hop_refused", "message": "repeated files proxy hop"},
        )
    origin = require_hub_daemon_url()
    client = DaemonClient.from_url(origin)
    headers = _forward_request_headers(request)
    params = dict(request.query_params.multi_items())
    try:
        if stream_body:
            return await _proxy_stream(
                client,
                request,
                headers=headers,
                params=params,
                accept_statuses=accept_statuses,
            )
        content = None if json_body is not None else await _buffered_body(request)
        response = await client.request_raw(
            request.method,
            request.url.path,
            headers=headers,
            params=params,
            content=content,
            json_data=json_body,
            hop=True,
            accept_statuses=accept_statuses,
        )
    except DaemonAuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except DaemonClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _as_route_result(response)


async def _buffered_body(request: Request) -> bytes | None:
    if request.method in {"GET", "HEAD", "DELETE"}:
        return None
    return await request.body()


def _forward_request_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    for name in FORWARD_REQUEST_HEADERS:
        value = request.headers.get(name)
        if value:
            headers[name] = value
    return headers


async def _proxy_stream(
    client: DaemonClient,
    request: Request,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, Any],
    accept_statuses: tuple[int, ...],
) -> Any:
    context = client.stream_request(
        request.method,
        request.url.path,
        headers=headers,
        params=params,
        content=request.stream(),
        hop=True,
        accept_statuses=accept_statuses,
    )
    response = await context.__aenter__()
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            await response.aread()
            return response.json()
        finally:
            await context.__aexit__(None, None, None)

    async def body() -> Any:
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await context.__aexit__(None, None, None)

    outbound = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in FORWARD_RESPONSE_HEADERS
    }
    return StreamingResponse(
        body(),
        status_code=response.status_code,
        headers=outbound,
        media_type=content_type or None,
    )


def _as_route_result(response: Any) -> Any:
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        return response.json()
    outbound = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in FORWARD_RESPONSE_HEADERS
    }
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=outbound,
        media_type=content_type or None,
    )
