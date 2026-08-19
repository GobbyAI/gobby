"""Owner-scope dispatch: local gateway on the hub, HTTP proxy on a node."""

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
from gobby.gwiki_gateway import INTERACTIVE_GWIKI_TIMEOUT_SECONDS, GwikiGateway
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.utils.daemon_client import DaemonAuthenticationError, DaemonClient, DaemonClientError
from gobby.wiki.owner_gateway import RemoteWikiGateway
from gobby.wiki.scope_resolution import (
    PERSONAL_SENTINELS,
    PROJECT_SCOPE_PREFIX,
    TOPIC_SCOPE_PREFIX,
    ResolvedWikiScope,
    _is_personal_sentinel,
    topic_scope,
)


def as_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise HTTPException(status_code=502, detail="hub returned a non-object JSON body")


def should_proxy_owner_scope(*, project: str | None, topic: str | None) -> bool:
    """True for remote topic/personal scopes. Does not stat local paths."""
    if not is_remote_files_mode():
        return False
    if topic is not None:
        return True
    if project is None:
        return False
    return _is_personal_sentinel(project)


def is_owner_watch_scope(scope: str) -> bool:
    value = scope.strip()
    if value.startswith(TOPIC_SCOPE_PREFIX):
        return True
    if value.startswith(PROJECT_SCOPE_PREFIX):
        value = value.removeprefix(PROJECT_SCOPE_PREFIX).strip()
    return value in PERSONAL_SENTINELS or value in {"personal", "_personal"}


def remote_scope(*, project: str | None, topic: str | None) -> ResolvedWikiScope:
    if topic is not None:
        identity = topic_scope(topic)
        return ResolvedWikiScope(
            identity=identity,
            topic=identity.removeprefix(TOPIC_SCOPE_PREFIX),
        )
    if project is not None and _is_personal_sentinel(project):
        return ResolvedWikiScope(
            identity=f"{PROJECT_SCOPE_PREFIX}{PERSONAL_PROJECT_ID}",
            project_id=PERSONAL_PROJECT_ID,
        )
    return ResolvedWikiScope(identity=None, project_id=project)


def gateway_for_resolved(
    resolved: ResolvedWikiScope,
    gateway_factory: Any | None = None,
    *,
    timeout_seconds: float = INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
) -> Any:
    if gateway_factory is not None:
        return gateway_factory(resolved)
    if _resolved_should_proxy(resolved):
        return RemoteWikiGateway(resolved, timeout_seconds=timeout_seconds)
    return GwikiGateway(
        project_root=resolved.project_root,
        topic=resolved.topic,
        timeout_seconds=timeout_seconds,
    )


def prune_gateway() -> Any:
    if is_remote_files_mode():
        return RemoteWikiGateway(
            ResolvedWikiScope(identity=None),
            timeout_seconds=INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
        )
    return GwikiGateway()


def _resolved_should_proxy(resolved: ResolvedWikiScope) -> bool:
    return should_proxy_owner_scope(project=resolved.project_id, topic=resolved.topic)


async def maybe_proxy_owner_request(
    request: Request,
    *,
    project: str | None = None,
    topic: str | None = None,
    stream_body: bool = False,
    accept_statuses: tuple[int, ...] = PROXY_ACCEPT_STATUSES,
) -> Any | None:
    if hop_header_present(request.headers) and is_remote_files_mode():
        raise HTTPException(
            status_code=409,
            detail={"error": "hop_refused", "message": "repeated files proxy hop"},
        )
    if not should_proxy_owner_scope(project=project, topic=topic):
        return None
    return await proxy_owner_request(
        request,
        stream_body=stream_body,
        accept_statuses=accept_statuses,
    )


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
        payload = response.json()
        await context.__aexit__(None, None, None)
        return payload

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
