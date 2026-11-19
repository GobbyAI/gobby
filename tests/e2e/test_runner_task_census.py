"""E2e runtime census of an isolated boot (WP-1; design-v2 4.1 C1c, 5.15 to 5.17; plan 1.2).

The boot runs the genuine runner through ``tests/e2e/task_census_bootstrap.py`` under the native
supervisor, with an isolated home and ports, against the isolated test hub only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e import conftest
from tests.e2e.task_census_recorder import (
    RECORD_TO_ENV_NAME,
    census_skip_reason,
    record_target_problem,
)

# The fixture was recorded on one host (R38, R41, R57). Off that host the module skips with a
# reason that names it, and the same checks run on every host through tests/contracts.
_SKIP_REASON = census_skip_reason()
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or ""),
]

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "tests" / "e2e" / "task_census_bootstrap.py"
BOOTSTRAP_MODULE = "tests.e2e.task_census_bootstrap"
INVENTORY = REPO / "tests" / "contracts" / "runner_lifecycle.json"
CENSUS = REPO / "tests" / "contracts" / "runner_census.json"
RECORD_TO_ENV = RECORD_TO_ENV_NAME
TEST_HUB_URL = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
SETTLE_TIMEOUT_SECONDS = 120.0
SETTLE_PAUSE_SECONDS = 2.0
CENSUS_LOG = Path("logs") / "task-census.jsonl"


def _settle(http_port: int) -> None:
    """Poll the unauthenticated health route until restart recovery is no longer pending."""
    deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
    url = f"http://127.0.0.1:{http_port}/api/health"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
            if not body.get("restart_recovery_pending", True):
                break
        except (OSError, ValueError):
            pass
        if time.monotonic() > deadline:
            pytest.fail("isolated census boot did not reach recovery-ready")
        time.sleep(0.5)
    time.sleep(SETTLE_PAUSE_SECONDS)


@pytest.fixture
def census_boot(
    e2e_project_dir: Path,
    e2e_config: tuple[Path, int, int],
    e2e_pre_daemon_setup: None,
) -> Generator[Path]:
    """Boot the census bootstrap to recovery-ready; yield the isolated GOBBY_HOME."""
    _ = e2e_pre_daemon_setup
    assert BOOTSTRAP.is_file(), f"{BOOTSTRAP.relative_to(REPO)} is absent"
    config_path, http_port, _ws_port = e2e_config
    daemon = conftest.spawn_daemon_instance(
        e2e_project_dir, e2e_config, runner_module=BOOTSTRAP_MODULE
    )
    try:
        instance = next(daemon)
        assert instance.is_alive()
        _settle(http_port)
        yield config_path.parent
    finally:
        next(daemon, None)


def _records(home: Path) -> list[dict[str, Any]]:
    lines = (home / CENSUS_LOG).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _mapped_keys() -> set[str]:
    """Census key strings that an inventory task lists, or that the census lists as outside."""
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    census = json.loads(CENSUS.read_text(encoding="utf-8"))
    mapped = {key for task in inventory["tasks"] for key in task["census"]}
    mapped.update(entry["key"] for entry in census["outside_runner_lifecycle"])
    return mapped


def _key_string(kind: str, creator: str, target: str) -> str:
    return f"{kind}|{creator}|{target}"


def test_census_bootstrap_refuses_unsafe_env(tmp_path: Path) -> None:
    """The bootstrap refuses without the test guard and refuses a non-test hub URL (1.2.1)."""
    assert BOOTSTRAP.is_file(), f"{BOOTSTRAP.relative_to(REPO)} is absent"
    base = {
        name: value
        for name, value in os.environ.items()
        if name not in {"GOBBY_TEST_PROTECT", "DATABASE_URL"}
    }
    base["PYTHONPATH"] = str(REPO / "src")
    config = tmp_path / "config.yaml"
    unguarded = subprocess.run(
        [sys.executable, str(BOOTSTRAP), "--config", str(config)],
        env={**base, "DATABASE_URL": TEST_HUB_URL},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert unguarded.returncode != 0
    assert "GOBBY_TEST_PROTECT" in unguarded.stderr
    foreign = subprocess.run(
        [sys.executable, str(BOOTSTRAP), "--config", str(config)],
        env={**base, "GOBBY_TEST_PROTECT": "1", "DATABASE_URL": "postgresql://other/daemon"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert foreign.returncode != 0
    assert "DATABASE_URL" in foreign.stderr


def test_isolated_boot_creates_only_inventoried_tasks(census_boot: Path) -> None:
    """The instrumented boot proves itself, then creates only inventoried tasks (1.2.2)."""
    from tests.e2e.task_census_recorder import record_key

    records = _records(census_boot)
    stages = {record.get("stage") for record in records if record.get("kind") == "stage"}
    assert {"construct", "serve"} <= stages, stages
    construct_tasks = [
        record
        for record in records
        if record.get("kind") == "task" and record.get("stage") == "construct"
    ]
    assert any(
        record["creator"] == "gobby.runner:_run_blocking_startup_phase"
        for record in construct_tasks
    )
    assert any(
        record.get("kind") == "task" and record.get("name") == "subsystem-init"
        for record in records
    )
    mapped = _mapped_keys()
    unmapped: list[str] = []
    for record in records:
        if record.get("kind") not in {"task", "thread"}:
            continue
        key = _key_string(*record_key(record))
        if key not in mapped:
            unmapped.append(f"{record['stage']}: {key}")
    assert not unmapped, sorted(unmapped)


def test_census_matches_fixture(census_boot: Path) -> None:
    """The live census equals the frozen fixture after normalization (R6, R23).

    Request-driven entries compare as a set of keys; everything else compares byte for byte.
    """
    from tests.e2e.task_census_recorder import build_census, comparison_view, dumps_fixture

    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    live = build_census(_records(census_boot), inventory=inventory)
    frozen = json.loads(CENSUS.read_text(encoding="utf-8"))
    assert dumps_fixture(comparison_view(live)) == dumps_fixture(comparison_view(frozen))


def test_census_recording(census_boot: Path, tmp_path: Path) -> None:
    """Write the normalized census and the raw records to GOBBY_CENSUS_RECORD_TO.

    Without the variable the record goes to the test's own tmp_path, so the boot is still
    captured. The record path must sit outside the real home (R24): the external-write audit
    watches ~/.gobby, so a recording there fails teardown. It must also sit outside the frozen
    fixture directory tests/contracts (R38), so a re-record cannot rewrite a fixture by accident. The raw records keep the line
    numbers that the normalized census drops; they sit beside the census as <stem>.raw.jsonl.
    """
    from tests.e2e.task_census_recorder import build_census, dumps_fixture

    target = os.environ.get(RECORD_TO_ENV)
    record = Path(target).resolve() if target else tmp_path / "census.json"
    problem = record_target_problem(record, repo=REPO, home=Path.home())
    if problem is not None:
        pytest.fail(problem)
    records = _records(census_boot)
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    record.write_text(dumps_fixture(build_census(records, inventory=inventory)), encoding="utf-8")
    raw = record.with_name(f"{record.stem}.raw.jsonl")
    raw.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in records), encoding="utf-8"
    )
    written = json.loads(record.read_text(encoding="utf-8"))
    assert raw.is_file(), raw
    assert written["tasks"], "the boot recorded no task"
    assert written["outside_runner_lifecycle"], "the boot recorded no outside entry"


def test_r12_no_stop_owner_recorded_with_debt() -> None:
    """Every observed task, thread, executor and outside service without a stop owner is recorded
    as it is, with the R0 debt note, and no other row carries the note (R12, R20)."""
    census = json.loads(CENSUS.read_text(encoding="utf-8"))
    observed_outside = [entry for entry in census["outside_runner_lifecycle"] if entry["observed"]]
    rows = [*census["tasks"], *census["threads"], *census["executors"], *observed_outside]
    assert rows
    for record in rows:
        owner = record["cancel_owner"] if "cancel_owner" in record else record["stop_owner"]
        label = record.get("key", record.get("creator"))
        assert record["debt"] in (None, "debt:R0"), label
        assert (owner is None) == (record["debt"] == "debt:R0"), label
