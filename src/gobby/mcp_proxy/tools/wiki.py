from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from gobby.gwiki_gateway import GwikiCommandError, GwikiGateway, GwikiGatewayError
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.wiki.scope_resolution import ResolvedWikiScope, resolve_wiki_scope
from gobby.wiki.update_coordinator import WikiUpdateCoordinator

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

_AI_VALUES = {"auto", "daemon", "direct", "off"}


class GwikiGatewayFactory(Protocol):
    def __call__(
        self,
        *,
        binary: str | None = None,
        project_root: str | Path | None = None,
        topic: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> GwikiGateway: ...


class WikiUpdateCoordinatorFactory(Protocol):
    def __call__(self, gateway: GwikiGateway) -> WikiUpdateCoordinator: ...


GatewayCall = Callable[[GwikiGateway], Awaitable[dict[str, Any]]]


def create_wiki_registry(
    *,
    db: HubDatabase | None = None,
    default_project_id: str | None = None,
    gateway_cls: GwikiGatewayFactory = GwikiGateway,
    update_coordinator_cls: WikiUpdateCoordinatorFactory = WikiUpdateCoordinator,
) -> InternalToolRegistry:
    registry = InternalToolRegistry(
        name="gobby-wiki",
        description=(
            "Wiki tools - wiki_search, wiki_ask, wiki_read, wiki_attach, wiki_ingest, "
            "wiki_compile, wiki_research, wiki_audit, wiki_trust, wiki_health, "
            "wiki_list_sources, wiki_remove_source"
        ),
    )

    async def gateway(
        project: str | None = None,
        topic: str | None = None,
    ) -> tuple[GwikiGateway, ResolvedWikiScope]:
        resolved = await resolve_wiki_scope(
            db,
            project=project,
            topic=topic,
            default_project_id=default_project_id,
        )
        gwiki = gateway_cls(
            binary=None,
            project_root=resolved.project_root,
            topic=resolved.topic,
            timeout_seconds=30.0,
        )
        return gwiki, resolved

    async def read_call(
        project: str | None, topic: str | None, call: GatewayCall
    ) -> dict[str, Any]:
        gwiki, scope = await gateway(project, topic)
        result = await _map_gateway_errors(lambda: call(gwiki))
        return _structured_result(result, scope=scope)

    async def write_call(
        project: str | None,
        topic: str | None,
        call: GatewayCall,
    ) -> dict[str, Any]:
        gwiki, scope = await gateway(project, topic)
        result = await _map_gateway_errors(lambda: call(gwiki))
        handled = await update_coordinator_cls(gwiki).handle_write_result(result)
        return _structured_result(handled, scope=scope)

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
        name="wiki_ask",
        description="Ask a question about the wiki. Read-only; optionally request LLM synthesis.",
    )
    async def wiki_ask(
        query: str,
        project: str | None = None,
        topic: str | None = None,
        llm: bool = False,
        ai: str | None = None,
        require_ai: bool = False,
    ) -> dict[str, Any]:
        ai_value = _normalize_ai(ai) if ai is not None else None
        return await _guard(
            lambda: read_call(
                project,
                topic,
                lambda gwiki: gwiki.ask(
                    query,
                    llm=llm,
                    ai=ai_value,
                    require_ai=require_ai,
                ),
            )
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
        name="wiki_research",
        description="Run wiki research enrichment, optionally in audit mode.",
    )
    async def wiki_research(
        query: str | None = None,
        project: str | None = None,
        topic: str | None = None,
        audit: bool = False,
        source_constraints: list[str] | None = None,
        max_steps: int | None = None,
        max_tokens: int | None = None,
        max_sources: int | None = None,
        ai: str = "daemon",
        require_ai: bool = False,
    ) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            if query is None and not audit:
                return _validation_error("query is required unless audit is true")
            ai_value = _normalize_ai(ai)
            _validate_positive_int("max_steps", max_steps)
            _validate_positive_int("max_tokens", max_tokens)
            _validate_positive_int("max_sources", max_sources)
            constraints = _string_sequence(source_constraints)
            return await write_call(
                project,
                topic,
                lambda gwiki: gwiki.research(
                    query,
                    audit=audit is True,
                    source_constraints=constraints,
                    max_steps=max_steps,
                    max_tokens=max_tokens,
                    max_sources=max_sources,
                    ai=ai_value,
                    require_ai=require_ai,
                ),
            )

        return await _guard(call)

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
        name="wiki_trust",
        description="Return wiki trust status.",
    )
    async def wiki_trust(
        project: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        return await _guard(lambda: read_call(project, topic, lambda gwiki: gwiki.trust()))

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
    scope: ResolvedWikiScope,
) -> dict[str, Any]:
    payload = result.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    structured = dict(result)
    structured["success"] = bool(result.get("ok", False))
    structured["scope"] = {
        "identity": scope.identity,
        "project": scope.project_id,
        "topic": scope.topic,
    }
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


def _normalize_ai(value: str | None) -> str:
    ai = (value or "daemon").strip().lower()
    if ai not in _AI_VALUES:
        raise ValueError("ai must be one of auto, daemon, direct, off")
    return ai


def _validate_positive_int(name: str, value: int | None) -> None:
    if value is None:
        return
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0")


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
    stderr: list[str] = []
    for result in results:
        payload = result.get("payload")
        if isinstance(payload, dict):
            changed_paths.extend(_changed_paths(payload))
        if isinstance(result.get("stderr"), str) and result["stderr"]:
            stderr.append(result["stderr"])
    return {
        "ok": all(bool(result.get("ok", False)) for result in results),
        "command": "ingest_file",
        "payload": {
            "command": "ingest-file",
            "results": results,
            "changed_paths": list(dict.fromkeys(changed_paths)),
        },
        "stderr": "\n".join(dict.fromkeys(stderr)),
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
