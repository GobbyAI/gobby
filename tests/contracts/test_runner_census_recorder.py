"""Unit tests for the runtime census recorder and its frozen fixture (WP-1; design-v2 5.15 to 5.17).

The recorder is pure: these tests pass synthetic frames and never start a daemon.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from gobby.config.bootstrap_io import read_present_bootstrap_mapping
from tests.e2e.task_census_bootstrap import (
    TEST_HUB_URL,
    BootstrapFileError,
    bootstrap_database_url,
    database_url_is_test_hub,
    real_home,
    unsafe_reason,
)
from tests.e2e.task_census_recorder import (
    LOOP_DEFAULT,
    RECORDED_PYTHON,
    TIMING_OPTIONAL_SUBMITTERS,
    _mapped_inventory_tasks,
    _placed_stop,
    build_census,
    census_skip_reason,
    creation_record,
    creator_from_frames,
    dumps_fixture,
    executor_record,
    normalize_name,
    normalize_record,
    record_key,
    record_target_problem,
    submit_record,
    timing_comparison_view,
)

REPO = Path(__file__).resolve().parents[2]
LOCALHOST_HUB_URL = "postgresql://gobby_test:gobby_test@localhost:60892/gobby_test"
ISOLATED_HUB_URL = (
    "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test_4242_abcdef"
    "?options=-csearch_path%3Dschema_x"
)
LIVE_LOOKING_URL = "postgresql://gobby:live-secret@127.0.0.1:5432/gobby"
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


def _guard_env(home: Path, database_url: str = TEST_HUB_URL) -> dict[str, str]:
    return {
        "GOBBY_TEST_PROTECT": "1",
        "DATABASE_URL": database_url,
        "GOBBY_HOME": str(home),
    }


def _write_bootstrap(home: Path, url: str | None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    database_line = "" if url is None else f"database_url: {url}\n"
    (home / "bootstrap.yaml").write_text(f"daemon_port: 1\n{database_line}", encoding="utf-8")


@pytest.mark.parametrize("url", [TEST_HUB_URL, LOCALHOST_HUB_URL, ISOLATED_HUB_URL])
def test_test_hub_dsn_accepts_loopback_forms(url: str) -> None:
    """The CI DSN names localhost, and the e2e bootstrap names an isolated database (R57)."""
    assert database_url_is_test_hub(url)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "postgresql://other/daemon",
        "postgresql://gobby_test:gobby_test@127.0.0.1:5432/gobby_test",
        "postgresql://gobby_test:gobby_test@db.example.com:60892/gobby_test",
        "postgresql://gobby:gobby_test@127.0.0.1:60892/gobby_test",
        "postgresql://gobby_test:other@127.0.0.1:60892/gobby_test",
        "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby",
        "mysql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test",
        "postgresql://gobby_test:gobby_test@127.0.0.1:notaport/gobby_test",
    ],
)
def test_test_hub_dsn_refuses_other_targets(url: str | None) -> None:
    assert not database_url_is_test_hub(url)


def test_guard_accepts_isolated_home_and_localhost_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-process accept case: a temporary operator home, and a bootstrap naming the hub."""
    operator = tmp_path / "operator"
    monkeypatch.setenv("HOME", str(operator))
    home = tmp_path / "census-home"
    _write_bootstrap(home, ISOLATED_HUB_URL)
    assert unsafe_reason(_guard_env(home, LOCALHOST_HUB_URL), operator_home=operator) is None


@pytest.mark.parametrize("nested", [False, True])
def test_guard_refuses_gobby_home_at_operator_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool
) -> None:
    operator = tmp_path / "operator"
    monkeypatch.setenv("HOME", str(operator))
    homes = [operator, operator / ".gobby"]
    if nested:
        homes.append(operator / ".gobby" / "logs")
    for home in homes:
        reason = unsafe_reason(_guard_env(home), operator_home=operator)
        assert reason is not None and "operator home" in reason, home


def test_guard_refuses_bootstrap_naming_another_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason names the file and never echoes the URL it refused."""
    operator = tmp_path / "operator"
    monkeypatch.setenv("HOME", str(operator))
    home = tmp_path / "census-home"
    _write_bootstrap(home, LIVE_LOOKING_URL)
    reason = unsafe_reason(_guard_env(home), operator_home=operator)
    assert reason is not None and "bootstrap.yaml" in reason
    assert "live-secret" not in reason and "5432" not in reason


@pytest.mark.parametrize("with_file", [False, True])
def test_guard_refuses_bootstrap_without_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_file: bool
) -> None:
    operator = tmp_path / "operator"
    monkeypatch.setenv("HOME", str(operator))
    home = tmp_path / "census-home"
    if with_file:
        _write_bootstrap(home, None)
    else:
        home.mkdir()
    reason = unsafe_reason(_guard_env(home), operator_home=operator)
    assert reason is not None and "bootstrap.yaml" in reason


def test_bootstrap_undecodable_bytes_are_refused(tmp_path: Path) -> None:
    """The daemon reads bootstrap.yaml as UTF-8, so a file with other bytes is refused even when
    its first database_url line names the test hub (R64)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "bootstrap.yaml").write_bytes(
        b'database_url: "' + TEST_HUB_URL.encode("ascii") + b'"\nopaque: \xff\xfe\n'
    )
    with pytest.raises(BootstrapFileError):
        bootstrap_database_url(home)


def test_bootstrap_indented_database_url_is_not_the_key(tmp_path: Path) -> None:
    """A database_url nested under another key is not the top-level value."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "bootstrap.yaml").write_text(
        f"daemon:\n  database_url: {TEST_HUB_URL}\n", encoding="utf-8"
    )
    assert bootstrap_database_url(home) is None


@pytest.mark.parametrize(
    "text",
    [
        f"database_url: {TEST_HUB_URL}\ndatabase_url: {LIVE_LOOKING_URL}\n",
        f'database_url: {TEST_HUB_URL}\n"database_url": {LIVE_LOOKING_URL}\n',
    ],
    ids=["repeated-key", "quoted-key"],
)
def test_guard_reads_the_last_duplicate_database_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """The daemon's yaml.safe_load keeps the last value of a repeated key, so a test-hub line
    followed by a live line is what the daemon reads: the guard reads the same value and refuses
    (R64). The agreement is checked against the daemon's own reader."""
    operator = tmp_path / "operator"
    monkeypatch.setenv("HOME", str(operator))
    home = tmp_path / "census-home"
    home.mkdir()
    path = home / "bootstrap.yaml"
    path.write_text(text, encoding="utf-8")
    assert bootstrap_database_url(home) == LIVE_LOOKING_URL
    assert bootstrap_database_url(home) == read_present_bootstrap_mapping(path)["database_url"]
    reason = unsafe_reason(_guard_env(home), operator_home=operator)
    assert reason is not None and "bootstrap.yaml" in reason
    assert "live-secret" not in reason and "5432" not in reason


@pytest.mark.parametrize(
    "text",
    [
        f"[\ndatabase_url: {TEST_HUB_URL}\n]\n",
        f"database_url: {TEST_HUB_URL}\nkey: [unclosed\n",
        "",
    ],
    ids=["sequence", "unparsable", "empty"],
)
def test_guard_refuses_bootstrap_that_is_not_a_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """A file the daemon refuses (not a YAML mapping, or not parsable) is refused by the guard,
    even when a column-zero database_url line in it names the test hub (R64)."""
    operator = tmp_path / "operator"
    monkeypatch.setenv("HOME", str(operator))
    home = tmp_path / "census-home"
    home.mkdir()
    (home / "bootstrap.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(BootstrapFileError):
        bootstrap_database_url(home)
    reason = unsafe_reason(_guard_env(home), operator_home=operator)
    assert reason is not None and "bootstrap.yaml" in reason
    assert "not a readable YAML mapping" in reason


def _loop_census(submitters: list[str], creator: str = LOOP_DEFAULT) -> dict[str, Any]:
    """A minimal census with one executor row and no outside entries."""
    return {
        "executors": [{"creator": creator, "prefix": "asyncio", "submitters": sorted(submitters)}],
        "outside_runner_lifecycle": [],
    }


def _same_under_timing_rule(live: dict[str, Any], frozen: dict[str, Any]) -> bool:
    return dumps_fixture(timing_comparison_view(live)) == dumps_fixture(
        timing_comparison_view(frozen)
    )


_MONITOR_CHECK = (
    "gobby.agents.interactive_attention_monitor:InteractiveAttentionMonitor._check_attention_panes"
)
_OTHER_SUBMITTER = "gobby.hooks.inbox:drain_hook_inbox_barrier"


def test_timing_optional_submitter_may_be_absent_or_present() -> None:
    """A monitor pass or a startup sweep may or may not have run before the read (R68)."""
    frozen = _loop_census([_OTHER_SUBMITTER, _MONITOR_CHECK])
    assert _same_under_timing_rule(_loop_census([_OTHER_SUBMITTER]), frozen)
    assert _same_under_timing_rule(_loop_census([_OTHER_SUBMITTER, _MONITOR_CHECK]), frozen)


def test_timing_rule_keeps_every_other_submitter_exact() -> None:
    """Only the named submitters are optional: an extra or missing other name still fails."""
    frozen = _loop_census([_OTHER_SUBMITTER, _MONITOR_CHECK])
    extra = _loop_census([_OTHER_SUBMITTER, _MONITOR_CHECK, "gobby.synthetic:unlisted"])
    assert not _same_under_timing_rule(extra, frozen)
    assert not _same_under_timing_rule(
        _loop_census([_MONITOR_CHECK]), _loop_census([_OTHER_SUBMITTER])
    )
    assert not _same_under_timing_rule(
        _loop_census([_OTHER_SUBMITTER, _MONITOR_CHECK]),
        _loop_census([_OTHER_SUBMITTER, "gobby.synthetic:unlisted"]),
    )


def test_timing_rule_applies_only_to_the_loop_default_executor() -> None:
    """The same submitter name on a pool the census keys by a creator is not optional."""
    frozen = _loop_census([], creator="gobby.synthetic.pool:make")
    live = _loop_census([_MONITOR_CHECK], creator="gobby.synthetic.pool:make")
    assert not _same_under_timing_rule(live, frozen)


def _defined_qualnames(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.add(f"{node.name}.{child.name}")
    return found


def test_timing_optional_names_are_not_in_the_fixture() -> None:
    """The optional set only admits extras: a name the frozen fixture lists is always exact."""
    listed = {
        str(name)
        for row in _census()["executors"]
        if row["creator"] == LOOP_DEFAULT
        for name in row["submitters"]
    }
    assert TIMING_OPTIONAL_SUBMITTERS.isdisjoint(listed), sorted(
        TIMING_OPTIONAL_SUBMITTERS & listed
    )


@pytest.mark.parametrize("submitter", sorted(TIMING_OPTIONAL_SUBMITTERS))
def test_timing_optional_names_are_real_definitions(submitter: str) -> None:
    """A renamed helper must fail here, so the optional set cannot silently stop matching."""
    module, qualname = submitter.split(":", 1)
    source = REPO / "src" / f"{module.replace('.', '/')}.py"
    assert qualname in _defined_qualnames(ast.parse(source.read_text(encoding="utf-8"))), submitter


def test_record_target_refuses_frozen_fixture_directory(tmp_path: Path) -> None:
    """Injected roots keep the check independent of where the worktree sits (R38, R57)."""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    fixture = repo / "tests" / "contracts" / "runner_census.json"
    problem = record_target_problem(fixture, repo=repo, home=home)
    assert problem is not None and "tests/contracts" in problem
    assert record_target_problem(repo / "tests" / "e2e" / "c.json", repo=repo, home=home) is None
    assert record_target_problem(tmp_path / "scratch" / "c.json", repo=repo, home=home) is None
    assert record_target_problem(home / "c.json", repo=repo, home=home) is not None


def test_census_module_skips_off_the_recorded_host() -> None:
    """The recorded host is darwin under CPython 3.14; any other platform or minor version skips
    the census with a reason that names it (R57). The skip is the exact minor version."""
    assert RECORDED_PYTHON == (3, 14)
    assert census_skip_reason(platform="darwin", version=(3, 14)) is None
    for platform, version in (("linux", (3, 14)), ("win32", (3, 14)), ("darwin", (3, 13))):
        reason = census_skip_reason(platform=platform, version=version)
        assert reason is not None and "macOS" in reason and "3.14" in reason, (platform, version)


def test_real_home_ignores_the_harness_home_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The e2e harness sets HOME to the isolated gobby home, so the operator's home must come
    from the account database, not from HOME (found by the census e2e run)."""
    isolated = tmp_path / "isolated"
    monkeypatch.setenv("HOME", str(isolated))
    assert real_home() != isolated
    assert real_home() != isolated.resolve()


def test_guard_accepts_the_harness_shaped_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HOME and GOBBY_HOME name the same isolated directory. That is an isolated home, not the
    operator's, so the guard accepts a bootstrap that names the isolated hub. Only the path
    comparison touches the operator's home; no file under it is read."""
    home = tmp_path / "isolated"
    monkeypatch.setenv("HOME", str(home))
    _write_bootstrap(home, ISOLATED_HUB_URL)
    assert unsafe_reason(_guard_env(home, LOCALHOST_HUB_URL), operator_home=real_home()) is None
