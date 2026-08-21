"""Native default flip and weekly-slot gate checker (plan 5.3)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
import yaml

from gobby.agents.spawn_models import resolve_terminal_backend
from gobby.config.app import DaemonConfig
from gobby.config.terminals import TerminalConfig, check_native_backend_flip
from gobby.storage.hub.protocol import HubDatabase
from gobby.terminals.discovery import seed_external_terminal
from gobby.terminals.host_client import HostCommandError
from gobby.terminals.runtime import PreparedSpawn, TerminalRuntime, TerminalSpawnRequest
from gobby.terminals.web_spawn import spawn_web_terminal
from tests.storage.test_terminals import LOCAL_MACHINE_ID, _manager

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_EVIDENCE = _REPO / "docs" / "evidence" / "native-backend-flip.md"
_CONFIG_YAML = _REPO / "src" / "gobby" / "install" / "shared" / "config" / "config.yaml"
_GUIDE = _REPO / "docs" / "guides" / "gterminal-development-guide.md"
_WORKFLOW = "Terminal Parity Weekly"
_QUERY = "label:terminal priority<=1 is_closed=false"


def _run_block(
    *,
    slot: str,
    timestamp: str,
    platform: str,
    run_id: str,
    sha: str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    package_install: str | None = "pass",
    focused_43: str | None = "pass",
    focused_36: str | None = "pass",
) -> str:
    lines = [
        "## Run",
        "",
        f"- workflow_name: `{_WORKFLOW}`",
        f"- weekly_slot: `{slot}`",
        f"- run_url: https://github.com/GobbyAI/gobby/actions/runs/{run_id}",
        f"- commit_sha: `{sha}`",
        f"- utc_timestamp: `{timestamp}`",
        f"- platform: `{platform}`",
    ]
    if package_install is not None:
        lines.append(f"- package_install: `{package_install}`")
    if focused_43 is not None:
        lines.append(f"- 4.3: `{focused_43}`")
    if focused_36 is not None:
        lines.append(f"- 3.6: `{focused_36}`")
    lines.append("")
    return "\n".join(lines) + "\n"


def _bugs(timestamp: str, *, count: int = 0, query: str = _QUERY) -> str:
    return (
        "## Open bugs\n"
        "\n"
        f"- count: `{count}`\n"
        f"- query: `{query}`\n"
        f"- query_timestamp: `{timestamp}`\n"
    )


def _slot_pair(
    slot_a: str,
    slot_b: str,
    ts_a: str,
    ts_b: str,
    bug_ts: str,
    *,
    platforms_a: tuple[str, ...] = ("macos-latest", "ubuntu-latest"),
    platforms_b: tuple[str, ...] = ("macos-latest", "ubuntu-latest"),
    omit_a: str | None = None,
    omit_b: str | None = None,
) -> str:
    parts = ["# Native backend flip\n\n"]
    run_id = 100
    for slot, timestamp, platforms, omit in (
        (slot_a, ts_a, platforms_a, omit_a),
        (slot_b, ts_b, platforms_b, omit_b),
    ):
        for platform in platforms:
            parts.append(
                _run_block(
                    slot=slot,
                    timestamp=timestamp,
                    platform=platform,
                    run_id=str(run_id),
                    package_install=None if omit == "package_install" else "pass",
                    focused_43=None if omit == "4.3" else "pass",
                    focused_36=None if omit == "3.6" else "pass",
                )
            )
            run_id += 1
    parts.append(_bugs(bug_ts))
    return "".join(parts)


def _conforming() -> str:
    return _slot_pair(
        "2026-W10",
        "2026-W11",
        "2026-03-02T06:17:00Z",
        "2026-03-09T06:17:00Z",
        "2026-03-09T18:00:00Z",
    )


@pytest.mark.asyncio
async def test_flip_preserves_explicit_and_external(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert TerminalConfig().default_backend == "native"
    daemon = DaemonConfig()
    assert daemon.terminals.default_backend == "native"
    loaded = yaml.safe_load(_CONFIG_YAML.read_text(encoding="utf-8"))
    assert loaded["terminals"]["default_backend"] == "native"
    guide = _GUIDE.read_text(encoding="utf-8")
    assert "default_backend" in guide
    assert '"tmux"' in guide or "`tmux`" in guide
    assert "rollback" in guide.lower()

    assert resolve_terminal_backend(None, daemon) == "native"
    assert resolve_terminal_backend("tmux", daemon) == "tmux"
    assert resolve_terminal_backend("native", daemon) == "native"

    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID)
    manager = _manager(temp_db)
    external = seed_external_terminal(
        manager,
        project_id=sample_project["id"],
        session_id=None,
        terminal_context={
            "tmux_socket_path": "/private/tmp/tmux-501/default",
            "tmux_pane": "%12",
            "tmux_session": "ext",
            "tmux_window": "@1",
            "tmux_server_pid": 1658,
            "tmux_server_start_time": 1784592177,
        },
        generation={
            "socket_path": "/private/tmp/tmux-501/default",
            "server_pid": 1658,
            "server_start_time": 1784592177,
            "pane_id": "%12",
        },
    )
    assert external is not None
    assert external.ownership == "external"
    assert external.backend == "tmux"

    prepares: list[str] = []
    entitlements = 0
    max_attachments_total = 8
    ceiling = max_attachments_total - 4

    class SaturatingNativeRuntime:
        backend = "native"

        async def reserve_observer(self, terminal_id: UUID) -> dict[str, str]:
            nonlocal entitlements
            if entitlements >= ceiling:
                raise HostCommandError("capacity")
            entitlements += 1
            return {
                "reservation_id": f"rsv-{terminal_id}",
                "reserve_key": str(terminal_id),
            }

        async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
            prepares.append(str(request.terminal_id))
            prepared = MagicMock()
            prepared.stored_locator = {"host_terminal_id": f"ht-{request.terminal_id}"}
            prepared.locator_key = f"native:epoch:ht-{request.terminal_id}"
            prepared.process = None
            prepared.host_terminal_id = f"ht-{request.terminal_id}"
            prepared.acknowledge_persist = MagicMock()
            prepared.acknowledge_observer = MagicMock()
            return prepared

        async def commit_spawn(self, prepared: PreparedSpawn) -> Any:
            del prepared
            return MagicMock(locator=MagicMock(frame_host_epoch="epoch-1"))

        async def terminate(self, terminal: object, grace: float) -> None:
            del terminal, grace
            nonlocal entitlements
            entitlements = max(0, entitlements - 1)

        async def bind_observer(self, prepared: PreparedSpawn, reservation_id: str) -> None:
            del prepared, reservation_id

    runtime = cast(TerminalRuntime, SaturatingNativeRuntime())
    for _ in range(ceiling):
        result = await spawn_web_terminal(
            manager=manager,
            runtime=runtime,
            project_id=sample_project["id"],
            session_id=None,
            rows=24,
            cols=80,
            cwd="/tmp",
            command=["zsh"],
        )
        assert result.success is True
    overflow = await spawn_web_terminal(
        manager=manager,
        runtime=runtime,
        project_id=sample_project["id"],
        session_id=None,
        rows=24,
        cols=80,
        cwd="/tmp",
        command=["zsh"],
    )
    assert overflow.success is False
    assert "capacity" in (overflow.error or "")
    assert str(overflow.terminal_id) not in prepares

    ws_source = (_REPO / "src" / "gobby" / "servers" / "websocket" / "terminal_ws.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(ws_source)
    create_fn = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if (
                    isinstance(item, ast.AsyncFunctionDef)
                    and item.name == "_handle_terminal_create"
                ):
                    create_fn = item
    assert create_fn is not None
    calls = [
        n.func.id
        for n in ast.walk(create_fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "spawn_web_terminal" in calls
    bind = [n.attr for n in ast.walk(create_fn) if isinstance(n, ast.Attribute)]
    assert "reserve_observer" not in bind
    assert "prepare_spawn" not in bind

    tmux_runtime = MagicMock()
    tmux_runtime.backend = "tmux"
    tmux_runtime.prepare_spawn = AsyncMock(
        return_value=MagicMock(
            stored_locator={
                "socket_path": "/tmp/tmux/default",
                "server_pid": 1,
                "server_start_time": 2,
                "pane_id": "%1",
            },
            locator_key="tmux:/tmp/tmux/default:1:2:%1",
            process=None,
            acknowledge_persist=MagicMock(),
        )
    )
    tmux_runtime.commit_spawn = AsyncMock(
        return_value=MagicMock(locator=MagicMock(frame_host_epoch=None))
    )
    explicit = await spawn_web_terminal(
        manager=manager,
        runtime=cast(TerminalRuntime, tmux_runtime),
        project_id=sample_project["id"],
        session_id=None,
        rows=24,
        cols=80,
        cwd="/tmp",
        command=["zsh"],
    )
    assert explicit.success is True
    row = manager.get(explicit.terminal_id)
    assert row is not None
    assert row.backend == "tmux"
    assert row.ownership == "gobby"


def test_flip_gate_rejects_every_nonconforming_artifact() -> None:
    assert TerminalConfig().default_backend == "native"
    cases: list[tuple[str, str, bool]] = [
        (
            "one_day_apart",
            _slot_pair(
                "2026-04-06",
                "2026-04-07",
                "2026-04-06T06:17:00Z",
                "2026-04-07T06:17:00Z",
                "2026-04-07T12:00:00Z",
            ),
            False,
        ),
        (
            "non_adjacent_same_year",
            _slot_pair(
                "2026-W10",
                "2026-W12",
                "2026-03-02T06:17:00Z",
                "2026-03-16T06:17:00Z",
                "2026-03-16T12:00:00Z",
            ),
            False,
        ),
        (
            "skipped_year_boundary",
            _slot_pair(
                "2025-W52",
                "2026-W02",
                "2025-12-22T06:17:00Z",
                "2026-01-05T06:17:00Z",
                "2026-01-05T12:00:00Z",
            ),
            False,
        ),
        (
            "complementary_one_platform_each",
            _slot_pair(
                "2026-W10",
                "2026-W11",
                "2026-03-02T06:17:00Z",
                "2026-03-09T06:17:00Z",
                "2026-03-09T18:00:00Z",
                platforms_a=("macos-latest",),
                platforms_b=("ubuntu-latest",),
            ),
            False,
        ),
        (
            "adjacent_missing_platform",
            _slot_pair(
                "2026-W10",
                "2026-W11",
                "2026-03-02T06:17:00Z",
                "2026-03-09T06:17:00Z",
                "2026-03-09T18:00:00Z",
                platforms_b=("macos-latest",),
            ),
            False,
        ),
        (
            "missing_4.3",
            _slot_pair(
                "2026-W10",
                "2026-W11",
                "2026-03-02T06:17:00Z",
                "2026-03-09T06:17:00Z",
                "2026-03-09T18:00:00Z",
                omit_b="4.3",
            ),
            False,
        ),
        (
            "missing_3.6",
            _slot_pair(
                "2026-W10",
                "2026-W11",
                "2026-03-02T06:17:00Z",
                "2026-03-09T06:17:00Z",
                "2026-03-09T18:00:00Z",
                omit_a="3.6",
            ),
            False,
        ),
        (
            "missing_package_install",
            _slot_pair(
                "2026-W10",
                "2026-W11",
                "2026-03-02T06:17:00Z",
                "2026-03-09T06:17:00Z",
                "2026-03-09T18:00:00Z",
                omit_b="package_install",
            ),
            False,
        ),
        (
            "bug_count_before_later_run",
            _slot_pair(
                "2026-W10",
                "2026-W11",
                "2026-03-02T06:17:00Z",
                "2026-03-09T06:17:00Z",
                "2026-03-09T06:16:59Z",
            ),
            False,
        ),
        (
            "bug_count_just_outside_24h",
            _slot_pair(
                "2026-W10",
                "2026-W11",
                "2026-03-02T06:17:00Z",
                "2026-03-09T06:17:00Z",
                "2026-03-10T06:17:01Z",
            ),
            False,
        ),
        (
            "same_year_numeric_weeks_not_consecutive_mondays",
            _slot_pair(
                "2025-W52",
                "2025-W53",
                "2025-12-22T06:17:00Z",
                "2025-12-29T06:17:00Z",
                "2025-12-29T12:00:00Z",
            ),
            False,
        ),
        ("conforming", _conforming(), True),
        (
            "iso_52_to_01",
            _slot_pair(
                "2025-W52",
                "2026-W01",
                "2025-12-22T06:17:00Z",
                "2025-12-29T06:17:00Z",
                "2025-12-29T18:00:00Z",
            ),
            True,
        ),
        (
            "iso_53_to_01",
            _slot_pair(
                "2020-W53",
                "2021-W01",
                "2020-12-28T06:17:00Z",
                "2021-01-04T06:17:00Z",
                "2021-01-04T18:00:00Z",
            ),
            True,
        ),
        (
            "exact_boundary_24h",
            _slot_pair(
                "2026-W10",
                "2026-W11",
                "2026-03-02T06:17:00Z",
                "2026-03-09T06:17:00Z",
                "2026-03-10T06:17:00Z",
            ),
            True,
        ),
        (
            "just_inside_24h",
            _slot_pair(
                "2026-W10",
                "2026-W11",
                "2026-03-02T06:17:00Z",
                "2026-03-09T06:17:00Z",
                "2026-03-10T06:16:59Z",
            ),
            True,
        ),
    ]
    for name, artifact, expect_ok in cases:
        result = check_native_backend_flip(artifact)
        assert result.ok is expect_ok, f"{name}: ok={result.ok} reasons={result.reasons}"

    committed = _EVIDENCE.read_text(encoding="utf-8")
    committed_result = check_native_backend_flip(committed)
    assert committed_result.ok, committed_result.reasons
    for field in (
        "workflow_name",
        "weekly_slot",
        "run_url",
        "commit_sha",
        "utc_timestamp",
        "platform",
        "package_install",
        "4.3",
        "3.6",
        "query",
        "query_timestamp",
    ):
        assert field in committed
