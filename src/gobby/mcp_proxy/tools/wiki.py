from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from gobby.config.app import DaemonConfig
from gobby.gwiki_gateway import GwikiCommandError, GwikiGateway, GwikiGatewayError
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.wiki.update_coordinator import WikiUpdateCoordinator


class GwikiGatewayFactory(Protocol):
    def __call__(
        self,
        *,
        binary: str | None = None,
        project: str | Path | None = None,
        topic: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> GwikiGateway: ...


class WikiUpdateCoordinatorFactory(Protocol):
    def __call__(self, gateway: GwikiGateway) -> WikiUpdateCoordinator: ...


GatewayCall = Callable[[GwikiGateway], Awaitable[dict[str, Any]]]


def create_wiki_registry(
    config: DaemonConfig | None = None,
    *,
    gateway_cls: GwikiGatewayFactory = GwikiGateway,
    update_coordinator_cls: WikiUpdateCoordinatorFactory = WikiUpdateCoordinator,
) -> InternalToolRegistry:
    registry = InternalToolRegistry(
        name="gobby-wiki",
        description=(
            "Wiki tools - wiki_search, wiki_read, wiki_attach, wiki_ingest, "
            "wiki_compile, wiki_audit, wiki_health, wiki_list_sources, wiki_remove_source"
        ),
    )

    def gateway(project: str | None = None, topic: str | None = None) -> GwikiGateway:
        if project is not None and topic is not None:
            raise ValueError("Provide project or topic scope, not both")
        wiki_config = getattr(config, "wiki", None)
        return gateway_cls(
            binary=getattr(wiki_config, "binary", None),
            project=project,
            topic=topic,
            timeout_seconds=float(getattr(wiki_config, "timeout_seconds", 30.0)),
        )

    async def read_call(
        project: str | None, topic: str | None, call: GatewayCall
    ) -> dict[str, Any]:
        gwiki = gateway(project, topic)
        result = await _map_gateway_errors(lambda: call(gwiki))
        return _structured_result(result, project=project, topic=topic)

    async def write_call(
        project: str | None,
        topic: str | None,
        call: GatewayCall,
    ) -> dict[str, Any]:
        gwiki = gateway(project, topic)
        result = await _map_gateway_errors(lambda: call(gwiki))
        handled = await update_coordinator_cls(gwiki).handle_write_result(result)
        return _structured_result(handled, project=project, topic=topic)

    @registry.tool(
        name="wiki_search",
        description="Search the wiki. Requires query; accepts project or topic scope and optional limit.",
    )
    async def wiki_search(
        query: str,
        project: str | None = None,
        topic: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return await _guard(
            lambda: read_call(project, topic, lambda gwiki: gwiki.search(query, limit=limit))
        )

    @registry.tool(
        name="wiki_read",
        description="Read a wiki page by exactly one of path or title.",
    )
    async def wiki_read(
        path: str | None = None,
        title: str | None = None,
        project: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        if (path is None) == (title is None):
            return _validation_error("Provide exactly one of path or title")
        return await _guard(
            lambda: read_call(project, topic, lambda gwiki: gwiki.read(path=path, title=title))
        )

    @registry.tool(
        name="wiki_attach",
        description="Attach an uploaded/staged file to the wiki by ingesting its daemon-local path.",
    )
    async def wiki_attach(
        path: str,
        project: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        return await _guard(
            lambda: write_call(project, topic, lambda gwiki: gwiki.ingest_file(path))
        )

    @registry.tool(
        name="wiki_ingest",
        description="Ingest one path, multiple paths, or a URL batch. Provide file input or urls, not both.",
    )
    async def wiki_ingest(
        path: str | None = None,
        paths: list[str] | None = None,
        urls: list[str] | None = None,
        project: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        file_paths = _ingest_paths(path, paths)
        url_values = _string_sequence(urls)
        if file_paths and url_values:
            return _validation_error("Provide file paths or URLs, not both")
        if not file_paths and not url_values:
            return _validation_error("Provide path, paths, or urls")

        if url_values:
            return await _guard(
                lambda: write_call(project, topic, lambda gwiki: gwiki.ingest_url(url_values))
            )
        if len(file_paths) == 1:
            return await _guard(
                lambda: write_call(project, topic, lambda gwiki: gwiki.ingest_file(file_paths[0]))
            )
        return await _guard(
            lambda: write_call(project, topic, lambda gwiki: _ingest_many(gwiki, file_paths))
        )

    @registry.tool(
        name="wiki_compile",
        description="Compile wiki content. Accepts optional output path.",
    )
    async def wiki_compile(
        output: str | None = None,
        project: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        return await _guard(lambda: write_call(project, topic, lambda gwiki: gwiki.compile(output)))

    @registry.tool(
        name="wiki_audit",
        description="Run wiki audit and hand write-like audit results to the index coordinator.",
    )
    async def wiki_audit(
        project: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        return await _guard(lambda: write_call(project, topic, lambda gwiki: gwiki.audit()))

    @registry.tool(
        name="wiki_health",
        description="Return wiki health payload.",
    )
    async def wiki_health(
        project: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        return await _guard(lambda: read_call(project, topic, lambda gwiki: gwiki.health()))

    @registry.tool(
        name="wiki_list_sources",
        description="List wiki sources using the same contract as GET /api/wiki/sources.",
    )
    async def wiki_list_sources(
        project: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        return await _guard(lambda: read_call(project, topic, lambda gwiki: gwiki.sources()))

    @registry.tool(
        name="wiki_remove_source",
        description=(
            "Remove or preview removal of a wiki source. Requires id; dry_run and yes are mutually exclusive."
        ),
    )
    async def wiki_remove_source(
        id: str,
        dry_run: bool = False,
        yes: bool = False,
        keep_asset: bool = False,
        project: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        if dry_run and yes:
            return _validation_error("dry_run and yes cannot both be true")
        effective_dry_run = dry_run or not yes
        return await _guard(
            lambda: write_call(
                project,
                topic,
                lambda gwiki: gwiki.remove_source(
                    id,
                    dry_run=effective_dry_run,
                    yes=yes,
                    keep_asset=keep_asset,
                ),
            )
        )

    return registry


async def _guard(call: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    try:
        return await call()
    except ValueError as exc:
        return _validation_error(str(exc))


async def _map_gateway_errors(call: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    return await _map_gateway_awaitable(call())


async def _map_gateway_awaitable(awaitable: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    try:
        return await awaitable
    except GwikiCommandError as exc:
        return exc.to_envelope()
    except GwikiGatewayError as exc:
        return {
            "ok": False,
            "status": "failed",
            "payload": None,
            "stderr": "",
            "error": {"type": exc.__class__.__name__, "message": str(exc)},
        }


def _structured_result(
    result: dict[str, Any],
    *,
    project: str | None,
    topic: str | None,
) -> dict[str, Any]:
    payload = result.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    structured = dict(result)
    structured["success"] = bool(result.get("ok", False))
    structured["scope"] = {"project": project, "topic": topic}
    structured["payload"] = payload
    structured["citations"] = _citations(payload)
    structured["paths"] = {
        "raw_paths": _raw_paths(payload),
        "changed_paths": _changed_paths(payload),
    }
    if isinstance(payload.get("content"), str):
        structured["content"] = payload["content"]
    return structured


def _validation_error(message: str) -> dict[str, Any]:
    return {"success": False, "ok": False, "error": message}


def _ingest_paths(path: str | None, paths: list[str] | None) -> list[str]:
    values: list[str] = []
    if path:
        values.append(path)
    values.extend(_string_sequence(paths))
    return list(dict.fromkeys(values))


async def _ingest_many(gateway: GwikiGateway, paths: list[str]) -> dict[str, Any]:
    results = []
    for path in paths:
        results.append(await _map_gateway_awaitable(gateway.ingest_file(path)))
    changed_paths: list[str] = []
    for result in results:
        payload = result.get("payload")
        if isinstance(payload, dict):
            changed_paths.extend(_changed_paths(payload))
    return {
        "ok": True,
        "command": "ingest_file",
        "payload": {
            "command": "ingest-file",
            "results": results,
            "changed_paths": list(dict.fromkeys(changed_paths)),
        },
        "stderr": "",
    }


def _citations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("citations")
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _raw_paths(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    _collect_paths(paths, payload, {"raw_path", "source_path", "path"})
    return list(dict.fromkeys(paths))


def _changed_paths(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    _collect_changed_paths(paths, payload)
    return list(dict.fromkeys(paths))


def _collect_changed_paths(paths: list[str], value: Any) -> None:
    if isinstance(value, dict):
        changed = value.get("changed_paths")
        for path in _string_sequence(changed):
            paths.append(path)
        for child in value.values():
            _collect_changed_paths(paths, child)
    elif isinstance(value, list):
        for item in value:
            _collect_changed_paths(paths, item)


def _collect_paths(paths: list[str], value: Any, keys: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, str):
                paths.append(child)
            else:
                _collect_paths(paths, child, keys)
    elif isinstance(value, list):
        for item in value:
            _collect_paths(paths, item, keys)


def _string_sequence(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list | tuple):
        return [item for item in value if isinstance(item, str) and item]
    return []
