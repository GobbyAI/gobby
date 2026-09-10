from __future__ import annotations

import argparse
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from tests.ask import native_probe_harness as harness
from tests.ask.test_native_probe_harness import (
    _SCHEMA,
    _SCOPED_TEST_DATABASE_URL,
    _TEST_DATABASE_URL,
)


@pytest.mark.parametrize("host_failure", [False, True])
def test_finalizer_cleans_latest_hosts_and_preserves_launch_group_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_failure: bool
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    output_dir = tmp_path / "output"
    calls: list[object] = []
    group = [{"pid": 321, "pgid": 321, "ppid": 1, "start_identity": "agent-start"}]
    agent = {
        "id": "agent",
        "pid": 321,
        "terminal_id": "terminal",
        "child_session_id": "child",
        "start_identity": "agent-start",
        "live": False,
        "terminal_process": {"pgid": 321},
    }
    launch = {**agent, "live": True, "process_group": group, "pgid": 321}
    hosts = {}
    for phase, pid, captured in [("fresh", 400, 1), ("resumed", 500, 2), ("recovery", 500, 3)]:
        hosts[phase] = {
            "captured_at_unix": captured,
            "workers": [],
            "agents": [],
            "hosts": [
                {
                    "phase": phase,
                    "host_pid": pid,
                    "start_identity": f"start-{pid}",
                    "host_epoch": f"epoch-{pid}",
                    "socket_dir": str(runtime_root / "host"),
                    "control_socket": "control",
                    "frames_socket": "frames",
                    "pidfile": "pid",
                    "live": True,
                    "process_group": [],
                    "control_socket_exists": True,
                    "frames_socket_exists": True,
                }
            ],
        }

    def terminate_host(snapshot: dict[str, Any], **_kwargs: object) -> dict[str, object]:
        host = snapshot["hosts"][0]
        calls.append(host["host_pid"])
        if host_failure:
            raise RuntimeError("host identity changed")
        return {
            "status": "terminated",
            "group_before": [],
            "group_after": [],
            "observation": {
                "workers": [],
                "agents": [],
                "hosts": [
                    {
                        **host,
                        "live": False,
                        "process_group": [],
                        "control_socket_exists": False,
                        "frames_socket_exists": False,
                    }
                ],
            },
        }

    def terminate_agent(row: dict[str, Any], **_kwargs: object) -> dict[str, object]:
        assert row["process_group"] == group
        return {"agent_run_id": "agent", "pid": 321, "group_before": group, "group_after": []}

    monkeypatch.setattr(harness, "_native_ask_execution_ids", lambda *_args: [])
    monkeypatch.setattr(harness, "_load_launch_receipts", lambda *_args: ({}, []))
    monkeypatch.setattr(
        harness, "_launch_process_snapshot", lambda *_args: {"workers": [], "agents": [launch]}
    )
    monkeypatch.setattr(harness, "_load_terminal_host_receipts", lambda *_args: (hosts, []))
    monkeypatch.setattr(
        harness,
        "_process_snapshot",
        lambda *_args, **_kwargs: {"workers": [], "agents": [dict(agent)]},
    )
    monkeypatch.setattr(harness, "_terminate_owned_host_process", terminate_host)
    monkeypatch.setattr(harness, "_terminate_owned_agent_process", terminate_agent)
    monkeypatch.setattr(harness, "_export_raw", lambda *_args, **_kwargs: {"complete": True})
    monkeypatch.setattr(harness, "_drop_owned_schema", lambda *_args: calls.append("schema"))
    monkeypatch.setattr(
        harness, "_remove_owned_runtime_root", lambda *_args: calls.append("runtime")
    )
    process_sets: dict[str, Any] = {}
    errors = harness._finalize_contained_probe(
        base_database_url=_TEST_DATABASE_URL,
        scoped_database_url=_SCOPED_TEST_DATABASE_URL,
        schema_name=_SCHEMA,
        schema_created=True,
        project_id="project",
        output_dir=output_dir,
        runtime_root=runtime_root,
        workers={},
        ask_run_ids=[],
        process_sets=process_sets,
        failure=None,
    )
    assert calls[:2] == [500, 400]
    assert bool(errors) is host_failure
    cleanup = json.loads((output_dir / "cleanup.json").read_bytes())
    if host_failure:
        assert calls == [500, 400]
        assert cleanup["runtime_root"]["status"] == "retained"
    else:
        assert calls == [500, 400, "schema", "runtime"]
        assert len(process_sets["after_cleanup"]["hosts"]) == 2
        assert process_sets["after_cleanup"]["agents"][0]["process_group"] == []


pytestmark = pytest.mark.unit


def test_owned_runtime_cleanup_removes_immutable_srt_without_following_symlinks(
    tmp_path: Path,
) -> None:
    runtime_root = harness._create_owned_runtime_root(parent=tmp_path)
    package = runtime_root / "gobby" / "tools" / "srt" / "0.0.66"
    package.mkdir(parents=True)
    (package / "runner.mjs").write_text("immutable runtime")
    (package / "runner.mjs").chmod(0o400)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("outside state")
    (package / "outside").symlink_to(outside, target_is_directory=True)
    outside.chmod(0o500)
    package.chmod(0o500)
    try:
        harness._remove_owned_runtime_root(runtime_root)
        assert not runtime_root.exists()
        assert outside.stat().st_mode & 0o777 == 0o500
        assert (outside / "keep.txt").read_text() == "outside state"
    finally:
        outside.chmod(0o700)
        if package.exists():
            package.chmod(0o700)
        if runtime_root.exists():
            shutil.rmtree(runtime_root)


def test_wait_reports_worker_error_before_nested_receipt_or_process_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error_path = tmp_path / "control" / "fresh-error.json"
    error_path.parent.mkdir()
    error_path.write_text(json.dumps({"error_type": "SrtRuntimeError", "message": "SRT missing"}))
    worker = cast(
        harness.OwnedWorker,
        SimpleNamespace(
            error_path=error_path,
            process=SimpleNamespace(poll=lambda: None),
        ),
    )

    def unexpected_sleep(_seconds: float) -> None:
        pytest.fail("wait ignored a terminal worker error receipt")

    monkeypatch.setattr(time, "sleep", unexpected_sleep)
    with pytest.raises(RuntimeError, match="SRT missing"):
        harness._wait_for_json(
            error_path.parent / "host-receipts" / "fresh.json",
            worker,
            deadline_monotonic=time.monotonic() + 60,
        )


@pytest.mark.asyncio
async def test_bootstrap_failure_rolls_back_created_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(lambda: None).result()
    closed: list[str] = []
    runner = SimpleNamespace(
        http_server=SimpleNamespace(services=SimpleNamespace()),
        bootstrap_config=SimpleNamespace(daemon_port=60001, websocket_port=60002),
        db_executor=SimpleNamespace(shutdown=executor.shutdown, join=lambda: None),
        database=SimpleNamespace(close=lambda: closed.append("database")),
    )
    monkeypatch.setenv("DATABASE_URL", _SCOPED_TEST_DATABASE_URL)
    monkeypatch.setattr("gobby.runner.GobbyRunner.create", AsyncMock(return_value=runner))
    monkeypatch.setattr(shutil, "which", lambda _name: "/probe/claude")
    monkeypatch.setattr(
        harness, "_bootstrap_policy_identity", AsyncMock(side_effect=RuntimeError("SRT missing"))
    )
    arguments = argparse.Namespace(
        timeout_seconds=60,
        config_path=tmp_path / "config.yaml",
        project_root=tmp_path,
        control_dir=tmp_path / "control",
        phase="fresh",
    )
    try:
        with pytest.raises(RuntimeError, match="SRT missing"):
            await harness._contained_worker_async(arguments)
        assert closed == ["database"]
        with pytest.raises(RuntimeError, match="cannot schedule new futures after shutdown"):
            executor.submit(lambda: None)
    finally:
        executor.shutdown()


@pytest.mark.parametrize(
    "failure",
    [
        "export_failed",
        "export_incomplete",
        "live_agent",
        "unknown_liveness",
        "snapshot_failed",
        "host_receipt_failed",
        "host_receipt_time",
    ],
)
def test_finalizer_retains_owned_state_until_export_and_death_are_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    evidence = runtime_root / "launch-policy.json"
    evidence.write_text("captured policy", encoding="utf-8")
    output_dir = tmp_path / "output"
    destructive_calls: list[str] = []

    def snapshot(*_args: object, **_kwargs: object) -> dict[str, Any]:
        if failure == "snapshot_failed":
            raise RuntimeError("snapshot unavailable")
        agents = []
        if failure in {"live_agent", "unknown_liveness"}:
            agents = [
                {"id": "agent", "pid": 321, "live": True if failure == "live_agent" else None}
            ]
        return {"workers": [], "agents": agents}

    def export(*_args: object, **_kwargs: object) -> dict[str, object]:
        if failure == "export_failed":
            raise RuntimeError("export unavailable")
        return {
            "path": "raw-probe.json",
            "sha256": "a" * 64,
            "complete": failure != "export_incomplete",
        }

    def host_receipts(*_args: object) -> tuple[dict[str, Any], list[dict[str, str]]]:
        if failure == "host_receipt_failed":
            raise OSError("receipt directory unavailable")
        if failure == "host_receipt_time":
            return {"fresh": {"captured_at_unix": "invalid"}}, []
        return {}, []

    monkeypatch.setattr(harness, "_native_ask_execution_ids", lambda *_args: [])
    monkeypatch.setattr(harness, "_process_snapshot", snapshot)
    monkeypatch.setattr(harness, "_load_terminal_host_receipts", host_receipts)
    monkeypatch.setattr(harness, "_terminate_owned_agent_process", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(harness, "_export_raw", export)
    monkeypatch.setattr(
        harness, "_drop_owned_schema", lambda *_args: destructive_calls.append("schema")
    )
    monkeypatch.setattr(
        harness, "_remove_owned_runtime_root", lambda *_args: destructive_calls.append("runtime")
    )

    errors = harness._finalize_contained_probe(
        base_database_url=_TEST_DATABASE_URL,
        scoped_database_url=_SCOPED_TEST_DATABASE_URL,
        schema_name=_SCHEMA,
        schema_created=True,
        project_id="project",
        output_dir=output_dir,
        runtime_root=runtime_root,
        workers={},
        ask_run_ids=[],
        process_sets={},
        failure=None,
    )

    assert destructive_calls == []
    assert errors
    assert evidence.read_text(encoding="utf-8") == "captured policy"
    cleanup = json.loads((output_dir / "cleanup.json").read_bytes())
    assert cleanup["schema"] == {"status": "retained", "name": _SCHEMA}
    assert cleanup["runtime_root"] == {"status": "retained", "path": str(runtime_root)}
