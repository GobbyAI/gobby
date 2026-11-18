"""DATABASE_URL guard: the shared truth table, per-run derivation and the dead-pid sweep.

The vectors in hub_guard_vectors.json are read by these tests and by the Rust guard tests,
so both engines answer every row the same way.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from tests.fixtures.hub_guard import (
    HARNESS_DATABASE_PATTERN,
    SHARED_PORTS,
    GuardMode,
    HubGuard,
    HubGuardError,
    check_derived_url,
    check_test_hub,
    dead_database_names,
    derive_run_url,
    harness_database_name,
    hub_database_url,
    maintenance_url,
    operator_home,
    sweep_dead_pid,
)
from tests.fixtures.shared_hub import shared_hub_lock_waits

pytestmark = pytest.mark.unit

_VECTORS_PATH = Path(__file__).with_name("hub_guard_vectors.json")
_DOCUMENT: dict[str, Any] = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))
_VECTORS: list[dict[str, Any]] = _DOCUMENT["vectors"]
_CHECK_VECTORS = [vector for vector in _VECTORS if vector["op"] == "check"]
_DERIVATION_VECTORS = [vector for vector in _VECTORS if vector["op"] != "check"]
_TEST_HUB_URL = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"


def _materialize_profile(spec: dict[str, Any] | None, tmp_path: Path) -> str | None:
    """Write the vector's rehearsal profile under tmp_path and return the path to pass."""
    if spec is None:
        return None
    if spec["gobby_home"] == "<operator_home>":
        home = operator_home()
    else:
        home = tmp_path / "gobby-home"
    if spec["content"] == "text":
        body = "not json\n"
    else:
        body = json.dumps(
            {
                "gobby_home": str(home),
                "postgres_host": spec["postgres_host"],
                "postgres_system_identifier": spec["postgres_system_identifier"],
                "services": {"postgres": {"port": spec["services_postgres_port"]}},
            }
        )
    target = tmp_path / "rehearsal-profile.json"
    target.write_text(body, encoding="utf-8")
    os.chmod(target, int(spec["mode"], 8))
    if spec["path"] == "absolute":
        return str(target)
    if spec["path"] == "relative":
        return f"relative/{target.name}"
    link = tmp_path / "profile-link.json"
    link.symlink_to(target)
    return str(link)


def test_vector_file_is_schema_one_with_unique_ids() -> None:
    assert _DOCUMENT["schema_version"] == 1
    ids = [vector["id"] for vector in _VECTORS]
    assert len(ids) == len(set(ids))
    for vector in _VECTORS:
        assert vector["expect"] in {"accept", "refuse"}
        if vector["expect"] == "refuse":
            assert vector["rule"] in {"R2", "R3", "R4", "ARG"}
            assert vector["mode"] == ""
        else:
            assert vector["rule"] == ""
            assert vector["mode"] in {"test_hub", "rehearsal"}


@pytest.mark.parametrize("vector", _CHECK_VECTORS, ids=[v["id"] for v in _CHECK_VECTORS])
def test_check_vector(vector: dict[str, Any], tmp_path: Path) -> None:
    profile_path = _materialize_profile(vector["profile"], tmp_path)
    env = dict(vector["env"])
    if vector["expect"] == "accept":
        guard = check_test_hub(vector["url"], env, profile_path=profile_path)
        assert guard.mode == GuardMode(vector["mode"])
        assert guard.base == vector["url"]
        return
    with pytest.raises(HubGuardError) as refusal:
        check_test_hub(vector["url"], env, profile_path=profile_path)
    assert refusal.value.rule == vector["rule"]


@pytest.mark.parametrize("vector", _DERIVATION_VECTORS, ids=[v["id"] for v in _DERIVATION_VECTORS])
def test_derivation_vector(vector: dict[str, Any]) -> None:
    try:
        if vector["op"] == "derive":
            guard = check_test_hub(vector["url"], {})
            name = harness_database_name(vector["pid"], vector["run_suffix"])
            derived = derive_run_url(guard, name)
            check_derived_url(derived, name, None, env={})
            assert name == vector["derived_dbname"]
        else:
            check_derived_url(
                vector["url"],
                vector["expected_dbname"],
                vector["expected_schema"],
                env={},
            )
    except HubGuardError as refusal:
        assert vector["expect"] == "refuse", str(refusal)
        assert refusal.rule == vector["rule"]
        return
    assert vector["expect"] == "accept"


def test_shared_ports_copy_equals_rehearsal_and_vector_file() -> None:
    from gobby.cli.hub_backup.rehearsal import _SHARED_PORTS

    assert SHARED_PORTS == frozenset(_SHARED_PORTS)
    assert frozenset(_DOCUMENT["shared_ports"]) == SHARED_PORTS


def test_derived_url_changes_only_the_database_path() -> None:
    guard = check_test_hub(_TEST_HUB_URL, {})
    name = harness_database_name(os.getpid(), "0123456789ab")
    assert derive_run_url(guard, name) == f"{_TEST_HUB_URL.rsplit('/', 1)[0]}/{name}"


def test_derived_url_appends_one_search_path_option() -> None:
    guard = check_test_hub(_TEST_HUB_URL, {})
    name = harness_database_name(os.getpid(), "0123456789ab")
    derived = derive_run_url(guard, name, schema="run_schema")
    assert derived.endswith(f"/{name}?options=-csearch_path%3Drun_schema")
    check_derived_url(derived, name, "run_schema", env={})


def test_rehearsal_derivation_returns_the_base_unchanged() -> None:
    base = "postgresql://gobby_test:gobby_test@127.0.0.1:61500/gobby_test"
    guard = HubGuard(base=base, mode=GuardMode.REHEARSAL)
    assert derive_run_url(guard, "gobby_harness_4242_0123456789ab") == base


def test_maintenance_url_targets_the_postgres_database() -> None:
    assert maintenance_url(_TEST_HUB_URL) == (
        "postgresql://gobby_test:gobby_test@127.0.0.1:60892/postgres"
    )


@pytest.mark.parametrize(
    ("pid", "token"),
    [(0, "0123456789ab"), (-1, "0123456789ab"), (10**7, "0123456789ab"), (4242, "0123456789AB")],
)
def test_harness_database_name_refuses_bad_pid_or_token(pid: int, token: str) -> None:
    with pytest.raises(HubGuardError) as refusal:
        harness_database_name(pid, token)
    assert refusal.value.rule == "ARG"


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def test_dead_database_names_match_the_whole_name_and_check_liveness() -> None:
    dead = _dead_pid()
    live = os.getpid()
    names = [
        f"gobby_harness_{dead}_0123456789ab",
        f"gobby_harness_{live}_0123456789ab",
        f"xgobby_harness_{dead}_0123456789ab",
        f"gobby_harness_{dead}_0123456789abc",
        "gobby_test",
    ]
    assert dead_database_names(names, pattern=HARNESS_DATABASE_PATTERN) == [names[0]]


def _create_database(url: str, name: str) -> None:
    with psycopg.connect(maintenance_url(url), autocommit=True) as connection:
        with shared_hub_lock_waits(connection, "CREATE DATABASE"):
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def _drop_database(url: str, name: str) -> None:
    with psycopg.connect(maintenance_url(url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


def _database_exists(url: str, name: str) -> bool:
    with psycopg.connect(maintenance_url(url), autocommit=True) as connection:
        row = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return row is not None


@pytest.mark.integration
def test_sweep_drops_a_planted_dead_pid_database() -> None:
    url = hub_database_url()
    name = harness_database_name(_dead_pid(), uuid.uuid4().hex[:12])
    _create_database(url, name)
    try:
        swept = sweep_dead_pid(maintenance_url(url), pattern=HARNESS_DATABASE_PATTERN)
        assert name in swept
        assert not _database_exists(url, name)
    finally:
        _drop_database(url, name)


@pytest.mark.integration
def test_sweep_keeps_a_database_whose_pid_is_alive() -> None:
    url = hub_database_url()
    name = harness_database_name(os.getpid(), uuid.uuid4().hex[:12])
    _create_database(url, name)
    try:
        swept = sweep_dead_pid(maintenance_url(url), pattern=HARNESS_DATABASE_PATTERN)
        assert name not in swept
        assert _database_exists(url, name)
    finally:
        _drop_database(url, name)


# C3: every worktree-daemon override site consults the guard. This block is the C3 red set.

_LIVE_HUB_URL = "postgresql://gobby:gobby@localhost:60891/gobby"
# Refused by R3 (wrong port), and never the live hub: the live coordinates stay out of
# the pytest process's environment, even for a test that only exercises a refusal.
_REFUSED_URL = "postgresql://gobby_test:gobby_test@127.0.0.1:60893/gobby_test"
_OVERRIDE = "GOBBY_ALLOW_WORKTREE_DAEMON"
_GUARD_AMBIENT = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "GOBBY_HUB_REHEARSAL_PROFILE")


@pytest.fixture(autouse=True)
def _guard_ambient_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep libpq redirects and a rehearsal profile in the process out of the os.environ tests."""
    for name in _GUARD_AMBIENT:
        monkeypatch.delenv(name, raising=False)


def test_guard_worktree_override_refuses_the_live_hub_argument() -> None:
    from tests.fixtures.hub_guard import guard_worktree_override

    env: dict[str, str] = {}
    assert guard_worktree_override(env, _LIVE_HUB_URL, None) is False
    assert _OVERRIDE not in env


def test_guard_worktree_override_sets_the_override_for_the_test_hub_argument() -> None:
    from tests.fixtures.hub_guard import guard_worktree_override

    env: dict[str, str] = {}
    assert guard_worktree_override(env, _TEST_HUB_URL, None) is True
    assert env[_OVERRIDE] == "1"


def test_guard_worktree_override_never_raises_without_a_url() -> None:
    from tests.fixtures.hub_guard import guard_worktree_override

    env: dict[str, str] = {}
    assert guard_worktree_override(env, None, None) is False
    assert _OVERRIDE not in env


def test_autouse_override_refuses_an_unauthorised_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import _allow_worktree_daemon

    monkeypatch.delenv(_OVERRIDE, raising=False)
    monkeypatch.setenv("DATABASE_URL", _REFUSED_URL)
    fixture: Any = _allow_worktree_daemon
    fixture.__wrapped__(monkeypatch)
    assert _OVERRIDE not in os.environ


def test_autouse_override_clears_an_inherited_override_on_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.conftest import _allow_worktree_daemon

    monkeypatch.setenv(_OVERRIDE, "1")
    monkeypatch.setenv("DATABASE_URL", _REFUSED_URL)
    fixture: Any = _allow_worktree_daemon
    fixture.__wrapped__(monkeypatch)
    assert _OVERRIDE not in os.environ


def test_autouse_override_is_set_for_the_test_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import _allow_worktree_daemon

    monkeypatch.delenv(_OVERRIDE, raising=False)
    monkeypatch.setenv("DATABASE_URL", _TEST_HUB_URL)
    fixture: Any = _allow_worktree_daemon
    fixture.__wrapped__(monkeypatch)
    assert os.environ[_OVERRIDE] == "1"


def test_lifecycle_site_refuses_the_live_hub_before_setting_override() -> None:
    from tests.e2e.test_daemon_lifecycle import _allow_worktree_daemon

    env: dict[str, str] = {}
    with pytest.raises(HubGuardError) as refusal:
        _allow_worktree_daemon(env, database_url=_LIVE_HUB_URL)
    assert refusal.value.rule == "R3"
    assert _OVERRIDE not in env


def test_lifecycle_site_sets_the_override_for_the_test_hub() -> None:
    from tests.e2e.test_daemon_lifecycle import _allow_worktree_daemon

    env: dict[str, str] = {}
    _allow_worktree_daemon(env, database_url=_TEST_HUB_URL)
    assert env[_OVERRIDE] == "1"


def test_composer_site_refuses_the_live_hub_and_leaves_env_alone(tmp_path: Path) -> None:
    from tests.e2e.test_composer_live_proof import composer_daemon_env

    env = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "key"}
    before = dict(env)
    with pytest.raises(HubGuardError) as refusal:
        composer_daemon_env(
            env,
            home=tmp_path / "home",
            config=tmp_path / "home" / "config.yaml",
            providers=tmp_path / "home" / "providers",
            trace_socket=tmp_path / "trace.sock",
            database_url=_LIVE_HUB_URL,
        )
    assert refusal.value.rule == "R3"
    assert env == before


def test_composer_site_builds_the_full_environment_for_the_test_hub(tmp_path: Path) -> None:
    from tests.e2e.test_composer_live_proof import composer_daemon_env

    home = tmp_path / "home"
    providers = home / "providers"
    config = home / "config.yaml"
    trace_socket = tmp_path / "trace.sock"
    env = composer_daemon_env(
        {
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "a",
            "OPENAI_API_KEY": "o",
            "GEMINI_API_KEY": "g",
        },
        home=home,
        config=config,
        providers=providers,
        trace_socket=trace_socket,
        database_url=_TEST_HUB_URL,
    )
    assert env == {
        "PATH": "/usr/bin",
        "GOBBY_CONFIG": str(config),
        "GOBBY_HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(providers / "claude"),
        "GOBBY_COMPOSER_PROOF_EXECUTION": "PD_AUTHORIZED_ISOLATED_EXECUTION",
        "GOBBY_COMPOSER_PROOF_SOCKET": str(trace_socket),
        _OVERRIDE: "1",
    }


def test_handoff_site_refuses_the_live_hub_and_leaves_env_alone(tmp_path: Path) -> None:
    from tests.e2e.handoff_continuation_fixture import handoff_daemon_env

    env = {"PATH": "/usr/bin", "OPENAI_API_KEY": "key"}
    before = dict(env)
    with pytest.raises(HubGuardError) as refusal:
        handoff_daemon_env(
            env,
            home=tmp_path / "home",
            config=tmp_path / "home" / "config.yaml",
            http_port=4321,
            trace_socket=tmp_path / "trace.sock",
            barrier_socket=None,
            database_url=_LIVE_HUB_URL,
        )
    assert refusal.value.rule == "R3"
    assert env == before


@pytest.mark.parametrize("with_barrier", [False, True])
def test_handoff_site_builds_the_full_environment_for_the_test_hub(
    tmp_path: Path, with_barrier: bool
) -> None:
    from tests.e2e.handoff_continuation_fixture import handoff_daemon_env

    home = tmp_path / "home"
    config = home / "config.yaml"
    trace_socket = tmp_path / "trace.sock"
    barrier_socket = tmp_path / "hold.sock" if with_barrier else None
    env = handoff_daemon_env(
        {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g"},
        home=home,
        config=config,
        http_port=4321,
        trace_socket=trace_socket,
        barrier_socket=barrier_socket,
        database_url=_TEST_HUB_URL,
    )
    expected = {
        "PATH": "/usr/bin",
        "GOBBY_CONFIG": str(config),
        "GOBBY_HOME": str(home),
        "GOBBY_DAEMON_URL": "http://127.0.0.1:4321",
        "GOBBY_COMPOSER_PROOF_EXECUTION": "PD_AUTHORIZED_ISOLATED_EXECUTION",
        "GOBBY_COMPOSER_PROOF_SOCKET": str(trace_socket),
        _OVERRIDE: "1",
    }
    if barrier_socket is not None:
        expected["GOBBY_HANDOFF_BARRIER_SOCKET"] = str(barrier_socket)
    assert env == expected
