from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gobby.gwiki_gateway import GwikiCommandError
from gobby.wiki.status import collect_wiki_status


class RecordingGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def status(self) -> dict[str, Any]:
        self.calls.append("status")
        return {
            "ok": True,
            "command": "status",
            "payload": {
                "status": "ready",
                "scope": {"project": "demo"},
                "indexed_paths": ["docs/wiki/hooks.md"],
            },
            "stderr": "",
        }

    async def health(self) -> dict[str, Any]:
        self.calls.append("health")
        return {
            "ok": True,
            "command": "health",
            "payload": {
                "status": "degraded",
                "degraded_services": ["embeddings"],
                "findings": [{"severity": "warning", "message": "Missing source"}],
            },
            "stderr": "",
        }


class FailingHealthGateway(RecordingGateway):
    async def health(self) -> dict[str, Any]:
        self.calls.append("health")
        raise GwikiCommandError(
            command="health",
            argv=("gwiki", "health"),
            returncode=2,
            stderr="gwiki health failed",
            payload={"status": "degraded", "degraded_services": ["index"]},
        )


class LiveWatcher:
    def __init__(self) -> None:
        self.calls = 0

    def health(self) -> dict[str, Any]:
        self.calls += 1
        return {
            "running": True,
            "scope_count": 2,
            "last_index_time": 1_779_999_001.25,
            "pending_debounce": True,
            "pending_changes": 3,
        }


@pytest.mark.asyncio
async def test_status_reports_maintenance_state() -> None:
    gateway = RecordingGateway()
    watcher = LiveWatcher()
    runner = SimpleNamespace(_wiki_watcher=watcher)

    status = await collect_wiki_status(gateway=gateway, runner=runner)

    payload = status["payload"]
    maintenance = payload["maintenance"]
    assert payload["status"] == "ready"
    assert maintenance["watcher"] == {
        "active": True,
        "running": True,
        "scope_count": 2,
        "last_index_time": 1_779_999_001.25,
        "pending_debounce": True,
        "pending_changes": 3,
    }
    assert maintenance["gateway"] == {
        "available": True,
        "status": "degraded",
        "degraded": True,
        "degraded_services": ["embeddings"],
        "error": None,
    }
    assert maintenance["degraded"] is True
    assert gateway.calls == ["status", "health"]
    assert watcher.calls == 1


@pytest.mark.asyncio
async def test_status_not_recorded_as_cron_history(temp_db: Any) -> None:
    before = temp_db.fetchone("SELECT COUNT(*) AS count FROM cron_runs")["count"]

    await collect_wiki_status(gateway=RecordingGateway(), runner=SimpleNamespace())

    after = temp_db.fetchone("SELECT COUNT(*) AS count FROM cron_runs")["count"]
    assert after == before


@pytest.mark.asyncio
async def test_status_reads_live_watcher_and_handles_absent_watcher() -> None:
    watcher = LiveWatcher()
    active = await collect_wiki_status(
        gateway=RecordingGateway(),
        runner=SimpleNamespace(_wiki_watcher=watcher),
    )
    inactive = await collect_wiki_status(
        gateway=FailingHealthGateway(),
        runner=SimpleNamespace(_wiki_watcher=None),
    )

    assert active["payload"]["maintenance"]["watcher"]["active"] is True
    assert active["payload"]["maintenance"]["watcher"]["pending_debounce"] is True
    assert watcher.calls == 1

    maintenance = inactive["payload"]["maintenance"]
    assert maintenance["watcher"] == {
        "active": False,
        "running": False,
        "scope_count": 0,
        "last_index_time": None,
        "pending_debounce": False,
        "pending_changes": 0,
    }
    assert maintenance["gateway"]["available"] is False
    assert maintenance["gateway"]["degraded"] is True
    assert maintenance["gateway"]["degraded_services"] == ["index"]
    assert maintenance["gateway"]["error"]["message"] == "gwiki health failed"
