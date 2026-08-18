from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from gobby.gwiki_gateway import (
    GENERATION_GWIKI_TIMEOUT_SECONDS,
    INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
    INTERACTIVE_HEALTH_GWIKI_TIMEOUT_SECONDS,
    MAX_URL_AGE_HOURS,
    GwikiCommandError,
    GwikiGateway,
    GwikiGatewayError,
    normalize_kind,
    normalize_page_write_mode,
)
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.sessions.transcript_archive import get_archive_dir
from gobby.wiki.owner_dispatch import gateway_for_resolved, remote_scope, should_proxy_owner_scope
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
        timeout_seconds: float = INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
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
    """Build the gobby-wiki MCP tool registry.

    Deliberately no ``wiki_graph`` tool: the full graph export is a multi-MB
    payload that would poison agent context as a tool result. Agents that need
    graph topology use ``gwiki graph`` artifacts and graph-context packs
    instead.
    """
    registry = InternalToolRegistry(
        name="gobby-wiki",
        description=(
            "Wiki tools - wiki_search, wiki_read, wiki_attach, wiki_ingest, "
            "wiki_write_page, wiki_delete_page, wiki_compile, wiki_audit, wiki_trust, "
            "wiki_health, wiki_list_sources, wiki_remove_source, wiki_sync_sessions"
        ),
    )

    async def gateway(
        project: str | None = None,
        topic: str | None = None,
        timeout_seconds: float = INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
    ) -> tuple[Any, ResolvedWikiScope]:
        if should_proxy_owner_scope(project=project, topic=topic):
            resolved = remote_scope(project=project, topic=topic)
            return gateway_for_resolved(resolved, timeout_seconds=timeout_seconds), resolved
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
            timeout_seconds=timeout_seconds,
        )
        return gwiki, resolved

    async def read_call(
        project: str | None,
        topic: str | None,
        call: GatewayCall,
        timeout_seconds: float = INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        gwiki, scope = await gateway(project, topic, timeout_seconds=timeout_seconds)
        result = await _map_gateway_errors(lambda: call(gwiki))
        return _structured_result(result, scope=scope)

    async def write_call(
        project: str | None,
        topic: str | None,
        call: GatewayCall,
        timeout_seconds: float = INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        gwiki, scope = await gateway(project, topic, timeout_seconds=timeout_seconds)
        result = await _map_gateway_errors(lambda: call(gwiki))
        if not result.get("ok", False):
            # Failed writes changed nothing; surface the error envelope without
            # a follow-up index decision (mirrors the HTTP route, which raises
            # before its coordinator step).
            return _structured_result(result, scope=scope)
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
        token_budget: int | None = None,
    ) -> dict[str, Any]:
        return await _guard(
            lambda: read_call(
                project,
                topic,
                lambda gwiki: gwiki.search(query, limit=limit, token_budget=token_budget),
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

    async def wiki_ingest(
        path: str | None = None,
        paths: list[str] | None = None,
        urls: list[str] | None = None,
        max_age_hours: int | None = None,
        project: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        file_paths = _ingest_paths(path, paths)
        url_values = _string_sequence(urls)
        if file_paths and max_age_hours is not None:
            return _validation_error("max_age_hours is only valid with urls")
        if max_age_hours is not None and not 0 <= max_age_hours <= MAX_URL_AGE_HOURS:
            return _validation_error(f"max_age_hours must be between 0 and {MAX_URL_AGE_HOURS}")
        if file_paths and url_values:
            return _validation_error("Provide file paths or URLs, not both")
        if not file_paths and not url_values:
            return _validation_error("Provide path, paths, or urls")

        if url_values:
            return await _guard(
                lambda: write_call(
                    project,
                    topic,
                    lambda gwiki: gwiki.ingest_url(
                        url_values,
                        max_age_hours=max_age_hours,
                    ),
                )
            )
        if len(file_paths) == 1:
            return await _guard(
                lambda: write_call(project, topic, lambda gwiki: gwiki.ingest_file(file_paths[0]))
            )
        return await _guard(
            lambda: write_call(project, topic, lambda gwiki: _ingest_many(gwiki, file_paths))
        )

    registry.register(
        name="wiki_ingest",
        description=(
            "Ingest one path, multiple paths, or a URL batch. Provide file input or urls, not both."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
                "urls": {"type": "array", "items": {"type": "string"}},
                "max_age_hours": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_URL_AGE_HOURS,
                },
                "project": {"type": "string"},
                "topic": {"type": "string"},
            },
            "required": [],
        },
        func=wiki_ingest,
    )

    @registry.tool(
        name="wiki_write_page",
        description=(
            "Write a wiki page under knowledge/ with content persisted verbatim. "
            "mode is upsert or create (create conflicts when the page already "
            "exists); expected_hash guards concurrent edits and fails with "
            "precondition_failed on mismatch."
        ),
    )
    async def wiki_write_page(
        path: str,
        content: str,
        mode: str = "upsert",
        expected_hash: str | None = None,
        project: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        async def run() -> dict[str, Any]:
            mode_value = normalize_page_write_mode(mode)
            return await write_call(
                project,
                topic,
                lambda gwiki: gwiki.write_page(
                    path=path,
                    content=content,
                    mode=mode_value,
                    expected_hash=expected_hash,
                ),
            )

        return await _guard(run)

    @registry.tool(
        name="wiki_delete_page",
        description="Delete a wiki page under knowledge/ and prune it from the index.",
    )
    async def wiki_delete_page(
        path: str,
        project: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        return await _guard(
            lambda: write_call(project, topic, lambda gwiki: gwiki.delete_page(path=path))
        )

    @registry.tool(
        name="wiki_compile",
        description=(
            "Compile accepted research notes into wiki articles. compile_topic sets "
            "the explicit article topic (distinct from the wiki-scope topic param) "
            "and always wins over checkpoint state; sources selects accepted source "
            "ids or paths wholesale; kind is one of source, concept, topic; outline "
            "seeds section headings; target overrides the output page path."
        ),
    )
    async def wiki_compile(
        compile_topic: str | None = None,
        kind: str | None = None,
        sources: list[str] | None = None,
        outline: list[str] | None = None,
        target: str | None = None,
        write_intent: bool = False,
        ai: str | None = None,
        project: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        async def run() -> dict[str, Any]:
            kind_value = _normalize_kind(kind)
            ai_value = _normalize_ai(ai) if ai is not None else None
            # Compile defaults to AI routing inside gwiki and its synthesis
            # scales with vault size, so it always gets the generation guard.
            return await write_call(
                project,
                topic,
                lambda gwiki: gwiki.compile(
                    compile_topic,
                    kind=kind_value,
                    sources=sources,
                    outline=outline,
                    target=target,
                    write_intent=write_intent,
                    ai=ai_value,
                ),
                timeout_seconds=GENERATION_GWIKI_TIMEOUT_SECONDS,
            )

        return await _guard(run)

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
        return await _guard(
            lambda: read_call(
                project,
                topic,
                lambda gwiki: gwiki.health(),
                timeout_seconds=INTERACTIVE_HEALTH_GWIKI_TIMEOUT_SECONDS,
            )
        )

    @registry.tool(
        name="wiki_sync_sessions",
        description="Sync archived daemon session transcripts into the wiki vault.",
    )
    async def wiki_sync_sessions(
        archive_dir: str | None = None,
        limit: int | None = None,
        project: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        effective_archive_dir = archive_dir
        if archive_dir is not None:
            archive_root = get_archive_dir().resolve(strict=False)
            candidate = Path(archive_dir).expanduser().resolve(strict=False)
            if candidate != archive_root and not candidate.is_relative_to(archive_root):
                return _validation_error("archive_dir must be inside the configured archive root")
            effective_archive_dir = str(candidate)
        return await _guard(
            lambda: write_call(
                project,
                topic,
                lambda gwiki: gwiki.sync_sessions(archive_dir=effective_archive_dir, limit=limit),
            )
        )

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


def _normalize_kind(value: str | None) -> str | None:
    return normalize_kind(value)


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
