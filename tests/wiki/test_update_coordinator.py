from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.gwiki_gateway import GwikiCommandError
from gobby.wiki.update_coordinator import WikiUpdateCoordinator

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class RecordingGateway:
    def __init__(
        self,
        *,
        scope: str | None = None,
        index_scopes: list[str] | None = None,
        index_result: dict[str, Any] | None = None,
        index_error: Exception | None = None,
    ) -> None:
        self.index_calls = 0
        self.scope = scope
        self.index_scopes = index_scopes
        self.index_result = index_result or {
            "ok": True,
            "command": "index",
            "payload": {"command": "index", "indexed": {"documents": 1}},
            "stderr": "",
        }
        self.index_error = index_error

    async def index(self) -> dict[str, Any]:
        self.index_calls += 1
        if self.scope is not None and self.index_scopes is not None:
            self.index_scopes.append(self.scope)
        if self.index_error is not None:
            raise self.index_error
        return self.index_result


class ScopedRecordingGateway:
    def __init__(self, scope: str, index_scopes: list[str]) -> None:
        self._scope = scope
        self._index_scopes = index_scopes

    async def index(self) -> dict[str, Any]:
        self._index_scopes.append(self._scope)
        return {"ok": True, "scope": self._scope}


def _result(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "command": command.replace("-", "_"),
        "payload": {"command": command, "scope": {"project": "/repo"}, **payload},
        "stderr": "",
    }


async def test_explicit_write_indexes_changed_paths() -> None:
    gateway = RecordingGateway()
    coordinator = WikiUpdateCoordinator(gateway)

    result = await coordinator.handle_write_result(
        _result("compile", {"changed_paths": ["docs/a.md"]})
    )

    assert gateway.index_calls == 1
    assert result["payload"]["changed_paths"] == ["docs/a.md"]
    assert result["index_handoff"] == {
        "status": "indexed",
        "changed_paths": ["docs/a.md"],
        "result": gateway.index_result,
    }


async def test_upkeep_write_indexes_written_cluster_pages() -> None:
    gateway = RecordingGateway()
    coordinator = WikiUpdateCoordinator(gateway)

    result = await coordinator.handle_write_result(
        _result(
            "upkeep",
            {
                "clusters": [
                    {"action": "created", "page_path": "knowledge/concepts/a.md"},
                    {"action": "updated", "page_path": "knowledge/concepts/b.md"},
                    {"action": "planned_create", "page_path": "knowledge/concepts/skip.md"},
                    {"action": "failed", "error": "boom"},
                ],
            },
        )
    )

    assert gateway.index_calls == 1
    assert result["index_handoff"]["status"] == "indexed"
    assert result["index_handoff"]["changed_paths"] == [
        "knowledge/concepts/a.md",
        "knowledge/concepts/b.md",
    ]


async def test_upkeep_dry_run_skips_index() -> None:
    gateway = RecordingGateway()
    coordinator = WikiUpdateCoordinator(gateway)

    result = await coordinator.handle_write_result(
        _result(
            "upkeep",
            {
                "dry_run": True,
                "clusters": [
                    {"action": "planned_create", "page_path": "knowledge/concepts/skip.md"}
                ],
            },
        )
    )

    assert gateway.index_calls == 0
    assert result["index_handoff"] == {"status": "skipped", "reason": "index_not_required"}


async def test_recap_write_indexes_page_path() -> None:
    gateway = RecordingGateway()
    coordinator = WikiUpdateCoordinator(gateway)

    result = await coordinator.handle_write_result(
        _result("recap", {"page_path": "recaps/2026-07-04.md", "page_action": "created"})
    )

    assert gateway.index_calls == 1
    assert result["index_handoff"]["status"] == "indexed"
    assert result["index_handoff"]["changed_paths"] == ["recaps/2026-07-04.md"]


async def test_recap_without_page_skips_index() -> None:
    gateway = RecordingGateway()
    coordinator = WikiUpdateCoordinator(gateway)

    result = await coordinator.handle_write_result(
        _result("recap", {"sessions_selected": 0, "synthesis": "skipped"})
    )

    assert gateway.index_calls == 0
    assert result["index_handoff"] == {"status": "skipped", "reason": "index_not_required"}


async def test_local_changes_index_each_changed_scope() -> None:
    index_scopes: list[str] = []

    def local_gateway(scope: str) -> RecordingGateway:
        return RecordingGateway(scope=scope, index_scopes=index_scopes)

    coordinator = WikiUpdateCoordinator(
        RecordingGateway(),
        local_gateway_factory=local_gateway,
    )

    result = await coordinator.handle_local_changes(
        {
            "project": [Path("/repo/wiki/a.md")],
            "topic:research": [Path("/topics/research/b.md")],
        }
    )

    assert index_scopes == ["project", "topic:research"]
    assert result["index_handoff"]["status"] == "indexed"
    assert result["index_handoff"]["changed_paths_by_scope"] == {
        "project": ["/repo/wiki/a.md"],
        "topic:research": ["/topics/research/b.md"],
    }
    assert set(result["index_handoff"]["results_by_scope"]) == {"project", "topic:research"}


async def test_index_failure_degrades() -> None:
    gateway = RecordingGateway(
        index_error=GwikiCommandError(
            command="index",
            argv=("gwiki", "index"),
            returncode=2,
            stderr="missing index store",
            payload={"degradations": [{"code": "missing_store"}]},
        )
    )
    coordinator = WikiUpdateCoordinator(gateway)

    result = await coordinator.handle_write_result(
        _result("ingest-file", {"changed_paths": ["raw/source.md"]})
    )

    assert gateway.index_calls == 1
    assert result["ok"] is True
    assert result["payload"]["command"] == "ingest-file"
    assert result["index_handoff"]["status"] == "degraded"
    assert result["index_handoff"]["degradation"] == {
        "type": "index_handoff_failed",
        "command": "index",
        "message": "missing index store",
        "stderr": "missing index store",
        "payload": {"degradations": [{"code": "missing_store"}]},
        "error": {"type": "command", "returncode": 2},
    }


@pytest.mark.parametrize(
    "command",
    ["status", "index", "search", "read", "backlinks", "audit", "health", "sources"],
)
async def test_read_only_operations_do_not_index(command: str) -> None:
    gateway = RecordingGateway()
    coordinator = WikiUpdateCoordinator(gateway)

    result = await coordinator.handle_write_result(
        _result(
            command, {"changed_paths": ["ignored.md"], "index_status": {"index_required": True}}
        )
    )

    assert gateway.index_calls == 0
    assert result["index_handoff"] == {"status": "skipped", "reason": "read_only_command"}


async def test_remove_source_indexes_only_when_required() -> None:
    gateway = RecordingGateway()
    coordinator = WikiUpdateCoordinator(gateway)

    skipped = await coordinator.handle_write_result(
        _result(
            "remove-source",
            {
                "removed_paths": ["raw/deleted.md"],
                "index_status": {"index_required": False},
            },
        )
    )
    indexed = await coordinator.handle_write_result(
        _result(
            "remove-source",
            {
                "removed_paths": [],
                "index_status": {"index_required": True},
            },
        )
    )

    assert gateway.index_calls == 1
    assert skipped["index_handoff"] == {"status": "skipped", "reason": "index_not_required"}
    assert indexed["index_handoff"]["status"] == "indexed"


async def test_local_changes_index_each_scope_with_scoped_gateway() -> None:
    index_scopes: list[str] = []
    coordinator = WikiUpdateCoordinator(
        RecordingGateway(),
        local_gateway_factory=lambda scope: ScopedRecordingGateway(scope, index_scopes),
    )

    result = await coordinator.handle_local_changes(
        {
            "project": [Path("/repo/wiki/a.md")],
            "topic:research": [Path("/topics/research/b.md")],
        }
    )

    assert index_scopes == ["project", "topic:research"]
    assert result["index_handoff"] == {
        "status": "indexed",
        "changed_paths_by_scope": {
            "project": ["/repo/wiki/a.md"],
            "topic:research": ["/topics/research/b.md"],
        },
        "results_by_scope": {
            "project": {"ok": True, "scope": "project"},
            "topic:research": {"ok": True, "scope": "topic:research"},
        },
    }


async def test_index_status_does_not_duplicate_handoff() -> None:
    gateway = RecordingGateway()
    coordinator = WikiUpdateCoordinator(gateway)

    result = await coordinator.handle_write_result(
        _result(
            "collect",
            {
                "changed_paths": ["raw/a.md"],
                "index_status": {"index_required": True},
            },
        )
    )

    assert gateway.index_calls == 1
    assert result["index_handoff"]["status"] == "indexed"


@pytest.mark.parametrize("command", ["ingest-url", "refresh"])
async def test_cli_indexed_batches_do_not_duplicate(command: str) -> None:
    gateway = RecordingGateway()
    coordinator = WikiUpdateCoordinator(gateway)

    result = await coordinator.handle_write_result(
        _result(
            command,
            {
                "changed_paths": ["raw/a.md"],
                "indexed": {"documents": 1},
                "index_status": {"index_required": True},
            },
        )
    )

    assert gateway.index_calls == 0
    assert result["payload"]["indexed"] == {"documents": 1}
    assert result["index_handoff"] == {"status": "skipped", "reason": "cli_indexed_batch"}


_TIMEOUT_ENVELOPE: dict[str, Any] = {
    "ok": False,
    "command": "index",
    "status": "degraded",
    "payload": None,
    "stderr": "",
    "error": {"type": "timeout", "message": "gwiki command timed out"},
}


async def test_write_result_envelope_failure_degrades() -> None:
    gateway = RecordingGateway(index_result=dict(_TIMEOUT_ENVELOPE))
    coordinator = WikiUpdateCoordinator(gateway)

    result = await coordinator.handle_write_result(
        _result("ingest-file", {"changed_paths": ["raw/source.md"]})
    )

    assert gateway.index_calls == 1
    assert result["index_handoff"]["status"] == "degraded"
    assert result["index_handoff"]["degradation"] == {
        "type": "index_handoff_failed",
        "command": "index",
        "message": "gwiki command timed out",
        "stderr": "",
        "payload": None,
        "error": {"type": "timeout"},
    }


async def test_local_changes_envelope_failure_degrades() -> None:
    gateway = RecordingGateway(index_result=dict(_TIMEOUT_ENVELOPE))
    coordinator = WikiUpdateCoordinator(gateway)

    result = await coordinator.handle_local_changes({"project": [Path("/repo/wiki/a.md")]})

    assert gateway.index_calls == 1
    handoff = result["index_handoff"]
    assert handoff["status"] == "degraded"
    assert handoff["changed_paths_by_scope"] == {"project": ["/repo/wiki/a.md"]}
    assert handoff["degradation"]["error"] == {"type": "timeout"}


async def test_scoped_local_changes_envelope_failure_degrades() -> None:
    def local_gateway(scope: str) -> RecordingGateway:
        if scope == "topic:research":
            return RecordingGateway(index_result=dict(_TIMEOUT_ENVELOPE))
        return RecordingGateway()

    coordinator = WikiUpdateCoordinator(
        RecordingGateway(),
        local_gateway_factory=local_gateway,
    )

    result = await coordinator.handle_local_changes(
        {
            "project": [Path("/repo/wiki/a.md")],
            "topic:research": [Path("/topics/research/b.md")],
            "topic:notes": [Path("/topics/notes/c.md")],
        }
    )

    handoff = result["index_handoff"]
    assert handoff["status"] == "degraded"
    assert handoff["failed_scope"] == "topic:research"
    assert set(handoff["results_by_scope"]) == {"project"}
    assert handoff["degradation"]["type"] == "index_handoff_failed"
    assert handoff["degradation"]["error"] == {"type": "timeout"}
