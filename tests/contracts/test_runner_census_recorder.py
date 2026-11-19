"""Unit tests for the runtime census recorder and its frozen fixture (WP-1; design-v2 5.15 to 5.17).

The recorder is pure: these tests pass synthetic frames and never start a daemon.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.task_census_recorder import (
    _mapped_inventory_tasks,
    _placed_stop,
    build_census,
    creation_record,
    creator_from_frames,
    executor_record,
    normalize_name,
    normalize_record,
    record_key,
    submit_record,
)

REPO = Path(__file__).resolve().parents[2]
CENSUS_PATH = REPO / "tests" / "contracts" / "runner_census.json"
LIFESPAN_CITES = {
    "src/gobby/servers/_app_lifecycle.py:98",
    "src/gobby/servers/_app_lifecycle.py:183",
    "src/gobby/servers/_app_lifecycle.py:246",
    "src/gobby/servers/_app_lifecycle.py:277",
    "src/gobby/servers/_app_lifecycle.py:307",
    "src/gobby/servers/_app_lifecycle.py:332",
}


def _census() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(CENSUS_PATH.read_text(encoding="utf-8"))
    return data


def _key_string(key: dict[str, str]) -> str:
    return f"{key['kind']}|{key['creator']}|{key['target']}"


def test_census_keeps_creator_target_cadence() -> None:
    census = _census()
    for record in [*census["tasks"], *census["unobserved"]]:
        assert record["creator"], record
        assert record["target"], record
        cadence = record["cadence"]
        if cadence is None:
            continue
        assert cadence["source"], record
        assert cadence["seconds"] is not None or (
            cadence["param"] is not None and cadence["default"] is not None
        ), record


def test_census_has_lifespan_monitors() -> None:
    """The lifespan cites are the observed set plus the static set (R21, R22), not a count."""
    entries = [entry for entry in _census()["outside_runner_lifecycle"] if entry["cite"]]
    assert {entry["cite"] for entry in entries} == LIFESPAN_CITES


def test_library_worker_thread_attributed() -> None:
    """A pool built by a library function, called from a gobby service, is that service's."""
    frames = [
        ("concurrent.futures.thread", "ThreadPoolExecutor.__init__"),
        ("third_party.lib.pool", "make_pool"),
        ("gobby.servers.service", "start_service"),
        ("gobby.runner", "run_gobby"),
    ]
    record = executor_record(frames, prefix="lib-pool", max_workers=2)
    assert record is not None
    assert record["creator"] == "gobby.servers.service:start_service"
    submit = submit_record(record["creator"], [("gobby.servers.service", "start_service")])
    assert submit is not None
    assert submit["submitter"] == "gobby.servers.service:start_service"


def test_executor_keyed_by_construction_frame() -> None:
    constructed = [
        ("concurrent.futures.thread", "ThreadPoolExecutor.__init__"),
        ("gobby.runner_init.services", "init_stateful_services"),
        ("gobby.runner", "create"),
    ]
    record = executor_record(constructed, prefix="gobby-embed", max_workers=1)
    assert record == {
        "kind": "executor",
        "creator": "gobby.runner_init.services:init_stateful_services",
        "prefix": "gobby-embed",
        "max_workers": 1,
        "stage": None,
        "line": None,
    }
    first = submit_record(record["creator"], [("gobby.runner", "create")])
    second = submit_record(record["creator"], [("gobby.runner", "create")])
    assert first == second
    assert first is not None and first["submitter"] == "gobby.runner:create"


def test_pool_thread_start_produces_no_record() -> None:
    frames = [("gobby.runner", "create"), ("concurrent.futures.thread", "_adjust")]
    assert (
        creation_record(
            "thread",
            frames,
            stage="construct",
            target=("concurrent.futures.thread", "_worker"),
            name="ThreadPoolExecutor-0_0",
            line=1,
        )
        is None
    )


def test_skip_rule_no_gobby_frame() -> None:
    frames = [("asyncio.base_events", "create_task"), ("tests.e2e.readiness_bootstrap", "main")]
    assert creator_from_frames(frames) is None
    assert (
        creation_record(
            "task",
            frames,
            stage="serve",
            target=("gobby.runner_maintenance.telemetry_loops", "span_cleanup_loop"),
            name="pytest-parent-watch",
            line=3,
        )
        is None
    )


def test_census_keys_exclude_line() -> None:
    frames = [("gobby.runner_lifecycle", "run_daemon")]
    early = creation_record(
        "task",
        frames,
        stage="serve",
        target=("gobby.runner_lifecycle", "_init_subsystems"),
        name="subsystem-init",
        line=286,
    )
    late = creation_record(
        "task",
        frames,
        stage="serve",
        target=("gobby.runner_lifecycle", "_init_subsystems"),
        name="subsystem-init",
        line=999,
    )
    assert early is not None and late is not None
    assert (
        record_key(early)
        == record_key(late)
        == (
            "task",
            "gobby.runner_lifecycle:run_daemon",
            "gobby.runner_lifecycle:_init_subsystems",
        )
    )
    for entry in _census()["tasks"]:
        assert set(entry["key"]) == {"kind", "creator", "target"}, entry["key"]


def test_normalization_drops_volatile_fields() -> None:
    record = {
        "kind": "thread",
        "stage": "serve",
        "creator": "gobby.runner_backend:watch_supervisor",
        "target": "gobby.runner_backend:read_pipe",
        "name": "Thread-17 (read_pipe)",
        "line": 170,
        "ident": 140735,
        "native_id": 99123,
        "pid": 4242,
        "port": 9002,
        "temp_path": "/var/folders/xy/T/pytest-of-josh/pytest-7/home",
        "timestamp": "2026-11-18T10:00:00Z",
        "uuid": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
    }
    normalized = normalize_record(record)
    assert set(normalized) == {"kind", "stage", "creator", "target", "name"}
    assert normalized["name"] == "Thread (read_pipe)"
    assert normalize_name("ThreadPoolExecutor-3_1") == "ThreadPoolExecutor"
    assert normalize_name("gobby-embed-lease_0") == "gobby-embed-lease"
    for entry in _census()["tasks"]:
        assert _key_string(entry["key"]).count("|") == 2, entry["key"]


def _synthetic_inventory(census_key: str, owner: dict[str, str] | None) -> dict[str, Any]:
    """One non-recurring inventory task that lists one census key, with no placements."""
    return {
        "tasks": [
            {
                "id": "synthetic-owner",
                "creator": {"module": "gobby.m", "qualname": "make"},
                "target": None,
                "cadence": None,
                "condition": None,
                "census": [census_key],
                "cancel_owner": owner,
                "disabled_in_profile": False,
            }
        ],
        "outside_placements": [],
    }


def test_executor_owner_ignores_worker_count() -> None:
    """The owner is looked up by creator and prefix, so a host-derived size cannot change it."""
    owner = {"module": "gobby.m", "qualname": "stop"}
    inventory = _synthetic_inventory("executor|gobby.m:make|p", owner)
    frames = [("concurrent.futures.thread", "ThreadPoolExecutor.__init__"), ("gobby.m", "make")]
    for workers in (4, 16):
        record = executor_record(frames, prefix="p", max_workers=workers)
        assert record is not None
        (row,) = build_census([record], inventory=inventory)["executors"]
        assert row["max_workers"] == workers
        assert row["cancel_owner"] == "gobby.m:stop"
        assert row["debt"] is None


def test_duplicate_census_key_is_refused() -> None:
    """A key listed by two inventory tasks would make its owner depend on file order."""
    inventory = {"tasks": [{"census": ["task|a|b"]}, {"census": ["task|a|b"]}]}
    with pytest.raises(ValueError, match="two inventory tasks"):
        _mapped_inventory_tasks(inventory)


def test_key_without_placement_is_debt() -> None:
    """A key with no placement row has no verified owner, so it carries the R0 debt tag."""
    assert _placed_stop("task|a|b", {}) == (None, "debt:R0")
    placement = {"task|a|b": {"stop_owner": "m:f", "debt": None}}
    assert _placed_stop("task|a|b", placement) == ("m:f", None)
