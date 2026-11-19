"""Census recorder helpers and the census builder (WP-1; design-v2 sections 5.15 to 5.17).

Nothing here starts a daemon or patches the runtime. ``tests/e2e/task_census_bootstrap.py``
installs the patches and writes the records these functions shape. The module is pure so the
unit tests can run it on synthetic frames.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

POOL_MODULE = "concurrent.futures.thread"
LOOP_DEFAULT = "loop-default"
# One timer family, two creator frames: HealthMonitor.start arms the first tick (threading.Timer
# at health_monitor.py:99) and the tick re-arms itself from its nested loop (:91). Which arm a
# boot observes varies, so both frames key as the start creator (R23).
CREATOR_ALIASES: dict[str, str] = {
    "gobby.hooks.health_monitor:HealthMonitor.start.<locals>.health_check_loop": (
        "gobby.hooks.health_monitor:HealthMonitor.start"
    ),
}
# Keys whose presence depends on request, probe or pool timing (R23). They are compared as a set.
REQUEST_DRIVEN_KEYS = frozenset(
    {
        "task|gobby.runner:main|uvicorn.protocols.http.httptools_impl:RequestResponseCycle.run_asgi",
        "task|gobby.runner:main|uvicorn.protocols.websockets.websockets_sansio_impl:WebSocketsSansIOProtocol.run_asgi",
        "task|gobby.runner:main|asyncio.selector_events:BaseSelectorEventLoop._accept_connection2",
        "task|gobby.servers.http:HTTPServer._capture_runtime_epoch|anyio._core._tasks:TaskHandle._run_coro",
        "task|gobby.servers.middleware.auth:AuthMiddleware.dispatch|anyio._core._tasks:TaskHandle._run_coro",
        "task|gobby.servers.middleware.project_context:ProjectContextMiddleware.dispatch|anyio._core._tasks:TaskHandle._run_coro",
        "task|gobby.telemetry.middleware:TelemetryMiddleware.dispatch|anyio._core._tasks:TaskHandle._run_coro",
        "task|gobby.storage.hub.async_ops:run_bounded_db|gobby.storage.hub.async_ops:_run_child",
        "thread|gobby.hooks.health_monitor:HealthMonitor.start|threading:Timer.run",
    }
)
# Environment keys the census bootstrap reads or sets (R25). Nothing else enters the baseline.
CENSUS_ENV_ALLOWLIST = frozenset({"GOBBY_HOME", "GOBBY_TEST_PROTECT"})
APP_LIFECYCLE_MODULE = "gobby.servers._app_lifecycle"
PROFILE_NAME = "e2e-hub"
PROFILE_RUN_MODE = "python"
PROFILE_ROUTE = "e2e-census"
# Host inputs the boot would otherwise read from the machine (finding 1 and finding 2). The
# bootstrap pins the CPU count that sizes the defaulted executors, and it puts a stub agy on PATH
# so the version probe spawns the same processes on every host. It also pins the child watcher to
# the threaded one: a host with pidfd starts no _do_waitpid thread, so the thread rows would
# depend on the kernel.
CENSUS_CPU_COUNT = 16
CENSUS_AGY_STUB = "census-bin/agy"
CENSUS_AGY_OUTPUT = "agy 1.0.0"
CENSUS_CHILD_WATCHER = "threaded"
PROFILE_CONFIG = {
    "fixture": "tests/e2e/conftest.py:e2e_config",
    "lines": "1093-1212",
    "disabled": ["code_index", "memory_dream"],
    "cpu_count": CENSUS_CPU_COUNT,
    "agy_stub": {"path": CENSUS_AGY_STUB, "output": CENSUS_AGY_OUTPUT},
    "child_watcher": CENSUS_CHILD_WATCHER,
}
PROFILE_BOOTSTRAP = "tests/e2e/task_census_bootstrap.py"
# Harness-owned threads: they have no gobby frame, so the skip rule never records them.
EXCLUDED = (
    {
        "name": "pytest-parent-watch",
        "cite": "tests/e2e/readiness_bootstrap.py:56",
        "reason": "started by the readiness harness; no gobby frame",
    },
    {
        "name": "startup-module-watch",
        "cite": "tests/e2e/readiness_bootstrap.py:113",
        "reason": "started by the readiness harness; no gobby frame",
    },
)
_NAME_SUFFIX = re.compile(r"[-_]\d+(?:_\d+)?(?=$| )")
_POOL_COUNTER = re.compile(r"pool-\d+-")


def is_gobby(module: str) -> bool:
    return module == "gobby" or module.startswith("gobby.")


def creator_from_frames(frames: Sequence[tuple[str, str]]) -> str | None:
    """The nearest gobby frame, innermost first; None when the stack has none (skip rule, GF-14)."""
    for module, qualname in frames:
        if is_gobby(module):
            return f"{module}:{qualname}"
    return None


def creation_record(
    kind: str,
    frames: Sequence[tuple[str, str]],
    *,
    stage: str,
    target: tuple[str, str],
    name: str | None,
    line: int,
    cite: str | None = None,
) -> dict[str, Any] | None:
    """A task or thread creation, or None when the skip rule or the pool rule drops it."""
    creator = creator_from_frames(frames)
    if creator is None:
        return None
    if kind == "thread" and target[0] == POOL_MODULE:
        # A pool worker's start is not a record: its executor and submitters are recorded instead.
        return None
    record: dict[str, Any] = {
        "kind": kind,
        "stage": stage,
        "creator": creator,
        "target": f"{target[0]}:{target[1]}",
        "name": name,
        "line": line,
    }
    if cite is not None:
        record["cite"] = cite
    return record


def record_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
    """(kind, creator, target). Line, name and stage are not part of the key; CREATOR_ALIASES
    collapses the creator frames of one timer family to one key."""
    creator = str(record["creator"])
    return (str(record["kind"]), CREATOR_ALIASES.get(creator, creator), str(record["target"]))


def key_string(key: tuple[str, str, str]) -> str:
    """The string form the inventory's ``census`` lists store."""
    return f"{key[0]}|{key[1]}|{key[2]}"


def executor_record(
    frames: Sequence[tuple[str, str]],
    *,
    prefix: str,
    max_workers: int | None,
    default_loop: bool = False,
    stage: str | None = None,
    line: int | None = None,
) -> dict[str, Any] | None:
    """An owned ThreadPoolExecutor, keyed by the frame that constructed it."""
    creator = LOOP_DEFAULT if default_loop else creator_from_frames(frames)
    if creator is None:
        return None
    return {
        "kind": "executor",
        "creator": creator,
        "prefix": prefix,
        "max_workers": max_workers,
        "stage": stage,
        "line": line,
    }


def submit_record(
    executor_creator: str, frames: Sequence[tuple[str, str]]
) -> dict[str, Any] | None:
    """A submit to an owned executor, keyed by its creator and the nearest gobby submitter."""
    submitter = creator_from_frames(frames)
    if submitter is None:
        return None
    return {"kind": "submit", "executor": executor_creator, "submitter": submitter}


def normalize_name(name: str) -> str:
    """Drop the run-specific counters: ``Thread-17 (x)``, ``Pool_3_1``, ``lease_0``, and the
    per-process pool number in ``pool-2-worker``."""
    return _POOL_COUNTER.sub("pool-", _NAME_SUFFIX.sub("", name))


def normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only the fields that are stable across boots."""
    name = record.get("name")
    return {
        "kind": record["kind"],
        "stage": record.get("stage"),
        "creator": record["creator"],
        "target": record["target"],
        "name": normalize_name(name) if isinstance(name, str) else None,
    }


def _mapped_inventory_tasks(inventory: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Census key string -> the one inventory task that lists it.

    A key listed twice would make the owner depend on file order, so it is refused here.
    """
    mapped: dict[str, dict[str, Any]] = {}
    for task in inventory["tasks"]:
        for key in task["census"]:
            if key in mapped:
                raise ValueError(f"census key listed by two inventory tasks: {key}")
            mapped[key] = task
    return mapped


def _owner_and_debt(
    text: str, mapped: Mapping[str, Mapping[str, Any]]
) -> tuple[str | None, str | None]:
    """The stop owner an inventory task gives a key, and the R0 debt tag when it has none."""
    task = mapped.get(text)
    owner = _owner_string(task["cancel_owner"]) if task is not None else None
    return owner, None if owner is not None else "debt:R0"


def _owner_string(owner: Mapping[str, str] | None) -> str | None:
    if owner is None:
        return None
    return f"{owner['module']}:{owner['qualname']}"


def _environment_keys(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    keys: list[str] = []
    for row in rows:
        if row.get("kind") == "environment":
            keys = sorted(str(item) for item in row.get("gobby_env_keys", []))
    return keys


def _task_rows(
    tasks: Mapping[str, dict[str, Any]], mapped: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for text, entry in tasks.items():
        owner = mapped.get(text)
        cancel = _owner_string(owner["cancel_owner"]) if owner is not None else None
        rows.append(
            {
                "key": entry["key"],
                "creator": entry["creator"],
                "target": entry["target"],
                "stage": entry["stage"],
                "names": sorted(entry["names"]),
                "cadence": owner["cadence"] if owner is not None else None,
                "observed": True,
                "cancel_owner": cancel,
                "debt": None if cancel is not None else "debt:R0",
            }
        )
    return sorted(rows, key=lambda row: (",".join(row["names"]), row["creator"], row["target"]))


def _thread_rows(
    threads: Mapping[str, dict[str, Any]], mapped: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for text, entry in threads.items():
        cancel, debt = _owner_and_debt(text, mapped)
        rows.append(
            {
                "key": entry["key"],
                "name": sorted(entry["names"]),
                "daemon": entry["daemon"],
                "creator": entry["creator"],
                "cancel_owner": cancel,
                "debt": debt,
            }
        )
    return sorted(
        rows, key=lambda row: (",".join(row["name"]), row["creator"], row["key"]["target"])
    )


def _executor_cite(creator: str, line: int | None) -> str | None:
    """The file and line of the constructing call; the loop default has no source line."""
    if creator == LOOP_DEFAULT or line is None:
        return None
    module = creator.split(":", 1)[0]
    return f"src/{module.replace('.', '/')}.py:{line}"


def _executor_rows(
    rows: Sequence[Mapping[str, Any]], owners: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    executors: dict[tuple[str, str, int | None], dict[str, Any]] = {}
    for row in rows:
        if row.get("kind") == "executor":
            workers = row["max_workers"]
            executor = (
                str(row["creator"]),
                str(row["prefix"]),
                None if workers is None else int(workers),
            )
            executors.setdefault(
                executor,
                {"stage": row.get("stage"), "line": row.get("line"), "submitters": set()},
            )
    for row in rows:
        if row.get("kind") == "submit":
            for executor, info in executors.items():
                if executor[0] == row["executor"]:
                    info["submitters"].add(str(row["submitter"]))
    # None (a defaulted pool size) sorts first; the sizes that were passed sort numerically.
    ordered = sorted(executors.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or -1))
    rows_out: list[dict[str, Any]] = []
    for (creator, prefix, workers), info in ordered:
        # The key carries no worker count: a defaulted size is host-derived, so keying on it
        # would let a host change flip an owner (finding 1). The size stays in the row.
        text = f"executor|{creator}|{prefix}"
        owner = owners.get(text, {}).get("owner")
        debt = None if owner is not None else owners.get(text, {}).get("debt", "debt:R0")
        rows_out.append(
            {
                "creator": creator,
                "prefix": prefix,
                "max_workers": workers,
                "stage": info["stage"],
                "cite": _executor_cite(creator, info["line"]),
                "submitters": sorted(info["submitters"]),
                "cancel_owner": owner,
                "debt": debt,
            }
        )
    return rows_out


def _placed_stop(
    text: str, placements: Mapping[str, Mapping[str, Any]]
) -> tuple[str | None, str | None]:
    """The stop owner and debt tag an outside placement row gives a key (R23).

    A key with no placement row has no verified owner, so it is recorded as debt.
    """
    placement = placements.get(text)
    if placement is None:
        return None, "debt:R0"
    return placement["stop_owner"], placement["debt"]


def _placed_entry(
    text: str, key: tuple[str, str, str], placement: Mapping[str, Any] | None
) -> dict[str, Any]:
    """An outside entry for a key the inventory does not own (R23): no lifespan cite."""
    if placement is None:
        # A request-driven key with no placement row: no owner is verified, so it is debt.
        return {
            "key": text,
            "cite": None,
            "observed": True,
            "request_driven": True,
            "timing_dependent": True,
            "creator": key[1],
            "target": key[2],
            "stop_owner": None,
            "debt": "debt:R0",
        }
    return {
        "key": text,
        "cite": None,
        "observed": True,
        "request_driven": bool(placement["request_driven"]),
        "timing_dependent": bool(placement["timing_dependent"]),
        "creator": key[1],
        "target": key[2],
        "stop_owner": placement["stop_owner"],
        "debt": placement["debt"],
    }


def _unobserved_rows(inventory: Mapping[str, Any], observed_keys: set[str]) -> list[dict[str, Any]]:
    """Recurring inventory entries whose creator and target were not seen in this boot.

    The match is on the task key itself, never on the census lists, which the per-key
    mapping stage fills later.
    """
    rows: list[dict[str, Any]] = []
    for task in sorted(inventory["tasks"], key=lambda item: item["id"]):
        cadence = task["cadence"]
        if cadence is None:
            continue
        creator = _owner_string(task["creator"])
        target = _owner_string(task["target"])
        if f"task|{creator}|{target}" in observed_keys:
            continue
        rows.append(
            {
                "id": task["id"],
                "creator": creator,
                "target": target,
                "cadence": cadence,
                "condition": task["condition"],
                "disabled_in_profile": bool(task.get("disabled_in_profile", False)),
                "cites": [cadence["source"]],
            }
        )
    return rows


# Lifespan creation sites in _app_lifecycle.py that a boot may not reach (R22). Each is
# emitted as an unobserved outside entry (observed false, key null) unless a boot sees it.
STATIC_LIFESPAN_CITES: dict[str, dict[str, Any]] = {
    "src/gobby/servers/_app_lifecycle.py:183": {
        "creator": (
            "gobby.servers._app_lifecycle:"
            "create_lifespan.<locals>.lifespan.<locals>._broadcast_session_change"
            ".<locals>._schedule"
        ),
        "target": "gobby.servers.websocket.broadcast:BroadcastMixin.broadcast_session_event",
        "cadence": {
            "config_key": None,
            "default": None,
            "param": None,
            "seconds": None,
            "source": "src/gobby/servers/_app_lifecycle.py:183",
            "wake": "event",
        },
    },
    "src/gobby/servers/_app_lifecycle.py:277": {
        "creator": "gobby.servers._app_lifecycle:create_lifespan.<locals>.lifespan",
        "target": (
            "gobby.servers._app_lifecycle:"
            "create_lifespan.<locals>.lifespan.<locals>._sync_existing_codex_sessions"
        ),
        "cadence": None,
    },
}


def build_census(
    records: Iterable[Mapping[str, Any]], *, inventory: Mapping[str, Any]
) -> dict[str, Any]:
    """The frozen census: observed tasks, threads and executors, unobserved recurring entries,
    the outside list (lifespan cites observed or static, and request-driven keys), and the
    environment and profile the boot ran under."""
    rows = [dict(record) for record in records]
    mapped = _mapped_inventory_tasks(inventory)
    placements: dict[str, Mapping[str, Any]] = {
        str(item["key"]): item for item in inventory.get("outside_placements", [])
    }
    executor_owners: dict[str, dict[str, Any]] = {
        text: {"owner": _owner_string(task["cancel_owner"]), "debt": None}
        for text, task in mapped.items()
        if text.startswith("executor|")
    }
    for text, placement in placements.items():
        if text.startswith("executor|"):
            executor_owners[text] = {
                "owner": placement["stop_owner"],
                "debt": placement["debt"],
            }
    tasks: dict[str, dict[str, Any]] = {}
    threads: dict[str, dict[str, Any]] = {}
    lifespan: dict[str, dict[str, Any]] = {}
    placed: dict[str, dict[str, Any]] = {}
    for row in rows:
        kind = row.get("kind")
        if kind not in {"task", "thread"}:
            continue
        key = record_key(row)
        text = key_string(key)
        if row.get("cite") is not None:
            cite = str(row["cite"])
            static = STATIC_LIFESPAN_CITES.get(cite)
            stop_owner, debt = _placed_stop(text, placements)
            lifespan.setdefault(
                cite,
                {
                    "key": text,
                    "cite": cite,
                    "observed": True,
                    "creator": key[1],
                    "target": key[2],
                    "cadence": static["cadence"] if static is not None else None,
                    "stop_owner": stop_owner,
                    "debt": debt,
                },
            )
            continue
        if text in placements or text in REQUEST_DRIVEN_KEYS:
            placed.setdefault(text, _placed_entry(text, key, placements.get(text)))
            continue
        target = threads if kind == "thread" else tasks
        entry = target.setdefault(
            text,
            {
                "key": {"kind": key[0], "creator": key[1], "target": key[2]},
                "creator": key[1],
                "target": key[2],
                "stage": row["stage"],
                "names": set(),
                "daemon": False,
            },
        )
        if row.get("name") is not None:
            entry["names"].add(normalize_name(str(row["name"])))
        if kind == "thread":
            entry["daemon"] = bool(row.get("daemon", False))
    for cite, static in STATIC_LIFESPAN_CITES.items():
        lifespan.setdefault(
            cite,
            {
                "key": None,
                "cite": cite,
                "observed": False,
                "creator": static["creator"],
                "target": static["target"],
                "cadence": static["cadence"],
                "stop_owner": None,
                "debt": None,
            },
        )
    observed_keys = set(tasks) | set(threads) | set(placed)
    return {
        "profile": {
            "name": PROFILE_NAME,
            "run_mode": PROFILE_RUN_MODE,
            "config_profile": PROFILE_CONFIG,
            "route": PROFILE_ROUTE,
            "bootstrap": PROFILE_BOOTSTRAP,
        },
        "environment": {"gobby_env_keys": _environment_keys(rows)},
        "tasks": _task_rows(tasks, mapped),
        "threads": _thread_rows(threads, mapped),
        "executors": _executor_rows(rows, executor_owners),
        "unobserved": _unobserved_rows(inventory, observed_keys),
        "outside_runner_lifecycle": [
            *(lifespan[cite] for cite in sorted(lifespan)),
            *(placed[text] for text in sorted(placed)),
        ],
        "excluded": [dict(item) for item in EXCLUDED],
    }


def comparison_view(census: Mapping[str, Any]) -> dict[str, Any]:
    """The census as compared by the fixture match (R23).

    Request-driven outside entries are reduced to the sorted set of their keys, so their
    presence is compared and their count and order are not.
    """
    outside = [dict(entry) for entry in census["outside_runner_lifecycle"]]
    view = dict(census)
    view["outside_runner_lifecycle"] = [
        entry for entry in outside if not entry.get("request_driven")
    ]
    view["request_driven_keys"] = sorted(
        str(entry["key"]) for entry in outside if entry.get("request_driven")
    )
    return view


def dumps_fixture(census: Mapping[str, Any]) -> str:
    """Canonical text of a census: sorted keys, two-space indent, one trailing newline."""
    return json.dumps(census, indent=2, sort_keys=True) + "\n"
