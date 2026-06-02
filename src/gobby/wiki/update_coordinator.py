from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from gobby.gwiki_gateway import GwikiCommandError, GwikiGatewayError


class GwikiIndexGateway(Protocol):
    async def index(self) -> dict[str, Any]:
        """Run same-scope gwiki indexing."""


READ_ONLY_COMMANDS = frozenset(
    {
        "status",
        "index",
        "search",
        "read",
        "backlinks",
        "audit",
        "health",
        "sources",
    }
)
EXPLICIT_WRITE_COMMANDS = frozenset(
    {
        "attach",
        "ingest-file",
        "ingest-url",
        "collect",
        "research",
        "compile",
        "remove-source",
        "refresh",
    }
)
CLI_INDEXED_BATCH_COMMANDS = frozenset({"ingest-url", "refresh"})


class WikiUpdateCoordinator:
    """Coordinates follow-up indexing for parsed gwiki write results."""

    def __init__(
        self,
        gateway: GwikiIndexGateway,
        *,
        local_gateway_factory: Callable[[str], GwikiIndexGateway] | None = None,
    ) -> None:
        self._gateway = gateway
        self._local_gateway_factory = local_gateway_factory or (lambda _scope: gateway)

    async def handle_local_changes(
        self, changed_paths_by_scope: dict[str, list[Path]]
    ) -> dict[str, Any]:
        changed_paths = {
            scope: [str(path) for path in paths] for scope, paths in changed_paths_by_scope.items()
        }
        results_by_scope: dict[str, dict[str, Any]] = {}
        degradations_by_scope: dict[str, dict[str, Any]] = {}

        for scope in changed_paths:
            try:
                results_by_scope[scope] = await self._local_gateway_factory(scope).index()
            except GwikiCommandError as exc:
                degradations_by_scope[scope] = _command_error_degradation(exc)
            except GwikiGatewayError as exc:
                degradations_by_scope[scope] = _gateway_error_degradation(exc)

        index_handoff: dict[str, Any] = {
            "status": "degraded" if degradations_by_scope else "indexed",
            "changed_paths_by_scope": changed_paths,
            "results_by_scope": results_by_scope,
        }
        if degradations_by_scope:
            index_handoff["degradations_by_scope"] = degradations_by_scope
            if len(degradations_by_scope) == 1:
                index_handoff["degradation"] = next(iter(degradations_by_scope.values()))
        if len(results_by_scope) == 1:
            index_handoff["result"] = next(iter(results_by_scope.values()))

        return {"index_handoff": index_handoff}

    async def handle_write_result(self, result: dict[str, Any]) -> dict[str, Any]:
        response = dict(result)
        payload = _payload(result)
        command = _command_name(result, payload)

        if command in READ_ONLY_COMMANDS or command not in EXPLICIT_WRITE_COMMANDS:
            response["index_handoff"] = {"status": "skipped", "reason": "read_only_command"}
            return response

        if command in CLI_INDEXED_BATCH_COMMANDS and payload.get("indexed") is not None:
            response["index_handoff"] = {"status": "skipped", "reason": "cli_indexed_batch"}
            return response

        changed_paths = [] if command == "remove-source" else _changed_paths(payload)
        if command == "remove-source":
            index_required = _index_required(payload)
        else:
            index_required = bool(changed_paths) or _index_required(payload)

        if not index_required:
            response["index_handoff"] = {"status": "skipped", "reason": "index_not_required"}
            return response

        try:
            index_result = await self._gateway.index()
        except GwikiCommandError as exc:
            response["index_handoff"] = {
                "status": "degraded",
                "changed_paths": changed_paths,
                "degradation": _command_error_degradation(exc),
            }
            return response
        except GwikiGatewayError as exc:
            response["index_handoff"] = {
                "status": "degraded",
                "changed_paths": changed_paths,
                "degradation": _gateway_error_degradation(exc),
            }
            return response

        response["index_handoff"] = {
            "status": "indexed",
            "changed_paths": changed_paths,
            "result": index_result,
        }
        return response


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("payload")
    return payload if isinstance(payload, dict) else {}


def _command_name(result: dict[str, Any], payload: dict[str, Any]) -> str:
    command = payload.get("command", result.get("command", ""))
    return str(command).replace("_", "-")


def _index_required(payload: dict[str, Any]) -> bool:
    index_status = payload.get("index_status")
    if not isinstance(index_status, dict):
        return False
    return index_status.get("index_required") is True


def _changed_paths(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    _extend_paths(paths, payload.get("changed_paths"))
    _extend_paths(paths, payload.get("written_paths"))
    _extend_paths(paths, payload.get("raw_path"))
    _extend_paths(paths, _nested_raw_path(payload.get("source")))
    _extend_entry_paths(paths, payload.get("accepted"))
    _extend_entry_paths(paths, payload.get("refreshed"), changed_only=True)
    return list(dict.fromkeys(paths))


def _extend_entry_paths(
    paths: list[str],
    entries: Any,
    *,
    changed_only: bool = False,
) -> None:
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if changed_only and entry.get("changed") is False:
            continue
        _extend_paths(paths, entry.get("raw_path"))
        _extend_paths(paths, _nested_raw_path(entry.get("source")))


def _nested_raw_path(value: Any) -> str | None:
    if isinstance(value, dict):
        raw_path = value.get("raw_path")
        return raw_path if isinstance(raw_path, str) else None
    return None


def _extend_paths(paths: list[str], value: Any) -> None:
    if isinstance(value, str):
        paths.append(value)
        return
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return
    for item in value:
        if isinstance(item, str):
            paths.append(item)


def _command_error_degradation(exc: GwikiCommandError) -> dict[str, Any]:
    return {
        "type": "index_handoff_failed",
        "command": exc.command,
        "message": str(exc),
        "stderr": exc.stderr,
        "payload": exc.payload,
        "error": {"type": "command", "returncode": exc.returncode},
    }


def _gateway_error_degradation(exc: GwikiGatewayError) -> dict[str, Any]:
    return {
        "type": "index_handoff_failed",
        "command": "index",
        "message": str(exc),
        "stderr": "",
        "payload": None,
        "error": {"type": exc.__class__.__name__},
    }
