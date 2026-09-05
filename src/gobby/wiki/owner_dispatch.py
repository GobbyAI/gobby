"""Owner-scope dispatch: local gateway on the hub, HTTP proxy on a node."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from gobby import files_home_proxy
from gobby.files_home_http import (
    PROXY_ACCEPT_STATUSES,
    hop_header_present,
    is_remote_files_mode,
)
from gobby.gwiki_gateway import INTERACTIVE_GWIKI_TIMEOUT_SECONDS, GwikiGateway
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.wiki.owner_gateway import RemoteWikiGateway
from gobby.wiki.scope_resolution import (
    PERSONAL_SENTINELS,
    PROJECT_SCOPE_PREFIX,
    TOPIC_SCOPE_PREFIX,
    ResolvedWikiScope,
    _is_personal_sentinel,
    topic_scope,
)


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
    return await files_home_proxy.proxy_owner_request(
        request,
        stream_body=stream_body,
        accept_statuses=accept_statuses,
    )
