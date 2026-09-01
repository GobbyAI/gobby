"""Observability must survive a checkout, a binary, and a hub that disagree."""

from __future__ import annotations

import ast
import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.cli.runtime import CliRuntime
from gobby.config.app import DaemonConfig
from gobby.runner_pid_file import ProbeState
from gobby.storage import schema_contract, schema_divergence
from gobby.storage.schema_contract import SchemaContractError
from gobby.storage.schema_divergence import (
    SchemaHeads,
    collect_schema_heads,
    installed_schema_identity,
    live_schema_version,
)

_IDENTITY = {
    "runner_protocol": 1,
    "baseline_version": 375,
    "baseline_checksum": "a" * 64,
    "latest_version": 414,
    "latest_checksum": "b" * 64,
    "assets_root_hash": "c" * 64,
}


@pytest.fixture(autouse=True)
def _isolated_native_bin_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the installed-set view (#21507) off the developer's real ~/.gobby/bin.

    ``start``/``restart``/``status`` probe every installed set member; an empty
    managed dir makes that view coherent so these tests exercise only the head
    divergence they stage.
    """
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(tmp_path / "native-bin"))


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gdaemon"], returncode=returncode, stdout=stdout, stderr=""
    )


def _database_returning(head: object) -> MagicMock:
    database = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = {"head": head}
    database.transaction.return_value.__enter__.return_value.execute.return_value = cursor
    return database


def _stub_config_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the config projection without querying the schema-head stub database.

    ``CliRuntime`` binds its repository factory as an ``__init__`` default, so patching
    the module attribute would leave the real repository in place for a runtime the CLI
    constructs itself. Patching the two methods covers both construction paths.
    """
    monkeypatch.setattr(
        "gobby.storage.config_repository.ConfigRepository.read",
        lambda self, resolve_secrets=False: SimpleNamespace(overrides={}, secret_bindings={}),
    )
    monkeypatch.setattr(
        "gobby.storage.config_repository.ConfigRepository.runtime_candidate",
        lambda self, overrides, bindings: DaemonConfig(),
    )


def test_agreeing_heads_render_without_a_divergence_marker() -> None:
    heads = SchemaHeads(checkout_version=413, installed_version=413, live_version=413)

    assert heads.diverged is False
    assert heads.describe() == "checkout pins v413 · installed gdaemon v413 · live hub v413"


def test_checkout_pin_behind_the_installed_binary_is_reported_not_raised() -> None:
    """Gate 1: crates/gdaemon/src/main.rs enforce_expected_identity would bail here."""
    heads = SchemaHeads(checkout_version=413, installed_version=414, live_version=413)

    assert heads.diverged is True
    assert heads.describe().endswith("— DIVERGED")
    assert "checkout pins v413 · installed gdaemon v414" in heads.describe()


def test_live_hub_ahead_of_the_runner_is_reported_not_raised() -> None:
    """Gate 2: crates/gcore/src/schema/runner.rs rejects a database ahead of the code."""
    heads = SchemaHeads(checkout_version=413, installed_version=413, live_version=417)

    assert heads.diverged is True
    assert heads.describe() == (
        "checkout pins v413 · installed gdaemon v413 · live hub v417 — DIVERGED"
    )


def test_unreadable_heads_are_named_and_do_not_count_as_divergence() -> None:
    heads = SchemaHeads(checkout_version=413, installed_version=None, live_version=413)

    assert heads.diverged is False
    assert "installed gdaemon unreadable" in heads.describe()


def test_installed_identity_reads_the_binary_without_the_expected_identity_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return _completed(json.dumps(_IDENTITY))

    monkeypatch.setattr(schema_divergence, "resolve_native_bin", lambda name: "/bin/gdaemon")
    monkeypatch.setattr(subprocess, "run", fake_run)

    identity = installed_schema_identity()

    assert identity is not None
    assert identity["latest_version"] == 414
    assert captured["args"] == ["/bin/gdaemon", "schema", "version", "--json"]
    assert "GOBBY_EXPECTED_SCHEMA_IDENTITY" not in captured["env"]


@pytest.mark.parametrize(
    "outcome",
    [
        _completed("", returncode=2),
        _completed("not json"),
        _completed(json.dumps({"latest_version": 414})),
    ],
    ids=["nonzero-exit", "unparseable", "invalid-identity"],
)
def test_installed_identity_degrades_to_none(
    monkeypatch: pytest.MonkeyPatch, outcome: subprocess.CompletedProcess[str]
) -> None:
    monkeypatch.setattr(schema_divergence, "resolve_native_bin", lambda name: "/bin/gdaemon")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: outcome)

    assert installed_schema_identity() is None


def test_installed_identity_is_none_without_an_installed_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schema_divergence, "resolve_native_bin", lambda name: None)

    assert installed_schema_identity() is None


def test_live_schema_version_reads_the_applied_head() -> None:
    assert live_schema_version(_database_returning(417)) == 417


def test_live_schema_version_degrades_on_an_unreadable_database() -> None:
    database = MagicMock()
    database.transaction.side_effect = RuntimeError("connection refused")

    assert live_schema_version(database) is None


def test_live_schema_version_is_none_on_an_unmigrated_database() -> None:
    assert live_schema_version(_database_returning(None)) is None


def test_collect_reports_every_head_it_can_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schema_divergence, "installed_schema_identity", lambda: _IDENTITY)
    monkeypatch.setattr(schema_divergence, "_checkout_version", lambda: 413)

    heads = collect_schema_heads(_database_returning(417))

    assert heads == SchemaHeads(checkout_version=413, installed_version=414, live_version=417)
    assert heads.diverged is True


def test_collect_without_a_database_omits_the_live_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schema_divergence, "installed_schema_identity", lambda: None)
    monkeypatch.setattr(schema_divergence, "_checkout_version", lambda: 413)

    heads = collect_schema_heads(None)

    assert heads == SchemaHeads(checkout_version=413, installed_version=None, live_version=None)


def test_checkout_version_reads_the_packaged_pin() -> None:
    packaged = json.loads(
        (Path("src/gobby/storage/schema_expected_identity.json")).read_text(encoding="utf-8")
    )

    assert schema_divergence._checkout_version() == packaged["latest_version"]


def test_read_only_operational_config_opens_the_database_without_migrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gobby status/health/stop must not run a schema apply to read three DB fields."""
    opened: list[bool] = []
    repository = MagicMock()
    repository.read.return_value = SimpleNamespace(overrides={}, secret_bindings={})
    repository.runtime_candidate.return_value = DaemonConfig()

    @contextmanager
    def open_database(_config_file: object, *, apply_migrations: bool) -> Iterator[MagicMock]:
        opened.append(apply_migrations)
        yield MagicMock()

    monkeypatch.setattr("gobby.cli.runtime.runtime_hub_database", open_database)
    runtime = CliRuntime(config_file=None, config_repository_factory=lambda _db: repository)

    config = runtime.read_only_operational_config()
    runtime.close()

    assert opened == [False]
    assert isinstance(config, DaemonConfig)


def test_operational_config_still_applies_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """gobby start stays fail-closed on the same accessor it uses today."""
    opened: list[bool] = []
    repository = MagicMock()
    repository.read.return_value = SimpleNamespace(overrides={}, secret_bindings={})
    repository.runtime_candidate.return_value = DaemonConfig()

    @contextmanager
    def open_database(_config_file: object, *, apply_migrations: bool) -> Iterator[MagicMock]:
        opened.append(apply_migrations)
        yield MagicMock()

    monkeypatch.setattr("gobby.cli.runtime.runtime_hub_database", open_database)
    runtime = CliRuntime(config_file=None, config_repository_factory=lambda _db: repository)

    config = runtime.operational_config
    runtime.close()

    assert opened == [True]
    assert isinstance(config, DaemonConfig)


def _config_accessors_by_function(source: str) -> dict[str, set[str]]:
    """Map each top-level function to the CliRuntime config accessors it reaches for."""
    tree = ast.parse(source)
    found: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        names: set[str] = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Attribute) and inner.attr == "operational_config":
                names.add("operational_config")
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "read_only_operational_config"
            ):
                names.add("read_only_operational_config")
        if names:
            found[node.name] = names
    return found


def test_observability_commands_read_config_without_applying_schema() -> None:
    daemon_source = Path("src/gobby/cli/daemon.py").read_text(encoding="utf-8")
    health_source = Path("src/gobby/cli/daemon_health.py").read_text(encoding="utf-8")

    daemon_accessors = _config_accessors_by_function(daemon_source)
    health_accessors = _config_accessors_by_function(health_source)

    for command in ("status", "_do_stop"):
        assert daemon_accessors[command] == {"read_only_operational_config"}, command
    assert health_accessors["health"] == {"read_only_operational_config"}


def test_start_still_applies_schema_before_running() -> None:
    source = Path("src/gobby/cli/daemon.py").read_text(encoding="utf-8")

    accessors = _config_accessors_by_function(source)

    assert accessors["start"] == {"operational_config"}


@pytest.mark.parametrize(
    ("relative_path", "expected"),
    [
        ("src/gobby/runner.py", "verify_schema"),
        ("src/gobby/cli/schema.py", "apply_schema"),
        ("src/gobby/storage/hub/runtime.py", "apply_destructive_migrations"),
    ],
)
def test_fail_closed_paths_still_call_the_identity_gate(relative_path: str, expected: str) -> None:
    source = Path(relative_path).read_text(encoding="utf-8")

    assert expected in source, relative_path


@dataclass(frozen=True)
class _Gate:
    """One real schema-gate rejection, with the inputs that produce it."""

    installed_latest: int
    live_head: int
    message: str


def _gate_cases() -> list[Any]:
    pin = schema_divergence._checkout_version()
    assert pin is not None
    return [
        pytest.param(
            _Gate(
                installed_latest=pin + 1,
                live_head=pin,
                message="expected schema identity does not match embedded identity",
            ),
            id="gate1-checkout-pin-behind-installed-binary",
        ),
        pytest.param(
            _Gate(
                installed_latest=pin,
                live_head=pin + 4,
                message=f"database schema v{pin + 4} is newer than this runner (v{pin})",
            ),
            id="gate2-live-hub-ahead-of-runner",
        ),
    ]


@pytest.fixture
def gated(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> _Gate:
    """Make the real gate reject any hub open that applies schema, and only that.

    Nothing about divergence is injected: the installed identity comes back through
    ``installed_schema_identity`` and the live head through ``live_schema_version``,
    so ``collect_schema_heads`` computes the disagreement from production inputs.
    """
    gate: _Gate = request.param
    identity = {**_IDENTITY, "latest_version": gate.installed_latest}

    monkeypatch.setattr(schema_divergence, "resolve_native_bin", lambda name: "/bin/gdaemon")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(json.dumps(identity)))

    @contextmanager
    def gated_hub(_config_file: object, *, apply_migrations: bool) -> Iterator[MagicMock]:
        if apply_migrations:
            raise SchemaContractError(gate.message)
        yield _database_returning(gate.live_head)

    monkeypatch.setattr("gobby.cli.runtime.runtime_hub_database", gated_hub)
    _stub_config_projection(monkeypatch)
    return gate


def _live_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    def probe(_path: object) -> SimpleNamespace:
        return SimpleNamespace(state=ProbeState.DAEMON, pid=4321)

    monkeypatch.setattr("gobby.cli.daemon.probe_daemon_lock", probe)
    monkeypatch.setattr("gobby.cli.daemon_health.probe_daemon_lock", probe)
    monkeypatch.setattr("gobby.cli.daemon._read_pid_file", lambda: 4321)
    monkeypatch.setattr("gobby.cli.daemon._is_process_alive", lambda _pid: True)
    monkeypatch.setattr("gobby.cli.daemon_health._is_process_alive", lambda _pid: True)
    monkeypatch.setattr("gobby.cli.daemon.get_port_listener_pid", lambda _port: 4321)
    monkeypatch.setattr("gobby.cli.daemon.get_service_status", dict)


@pytest.mark.parametrize("gated", _gate_cases(), indirect=True)
def test_status_survives_the_gate_and_names_the_divergence(
    monkeypatch: pytest.MonkeyPatch, gated: _Gate
) -> None:
    _live_daemon(monkeypatch)
    pin = schema_divergence._checkout_version()

    result = CliRunner().invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert gated.message not in result.output
    assert (
        f"checkout pins v{pin} · installed gdaemon v{gated.installed_latest} · "
        f"live hub v{gated.live_head} — DIVERGED"
    ) in result.output


@pytest.mark.parametrize("gated", _gate_cases(), indirect=True)
def test_health_survives_the_gate_and_names_the_divergence(
    monkeypatch: pytest.MonkeyPatch, gated: _Gate
) -> None:
    _live_daemon(monkeypatch)
    monkeypatch.setattr(
        "gobby.cli.daemon_health.httpx.get",
        lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {"status": "ok"}),
    )

    result = CliRunner().invoke(cli, ["health"])

    assert result.exit_code == 0, result.output
    assert "DIVERGED" in result.output
    assert "Gobby daemon: healthy" in result.output
    assert gated.message not in result.output


@pytest.mark.parametrize("gated", _gate_cases(), indirect=True)
def test_a_migrating_config_read_still_raises_under_the_same_gate(gated: _Gate) -> None:
    """The accessor gobby start uses must keep failing closed on both divergences."""
    runtime = CliRuntime(config_file=None)

    with pytest.raises(SchemaContractError) as excinfo:
        assert runtime.operational_config

    assert gated.message in str(excinfo.value)
    runtime.close()


@pytest.mark.parametrize("gated", _gate_cases(), indirect=True)
def test_the_read_only_accessor_passes_the_same_gate(gated: _Gate) -> None:
    runtime = CliRuntime(config_file=None)

    assert isinstance(runtime.read_only_operational_config(), DaemonConfig)
    runtime.close()


def test_health_stays_a_one_liner_when_every_head_agrees(monkeypatch: pytest.MonkeyPatch) -> None:
    pin = schema_divergence._checkout_version()
    assert pin is not None
    monkeypatch.setattr(schema_divergence, "resolve_native_bin", lambda name: "/bin/gdaemon")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _completed(json.dumps({**_IDENTITY, "latest_version": pin})),
    )

    @contextmanager
    def hub(_config_file: object, *, apply_migrations: bool) -> Iterator[MagicMock]:
        yield _database_returning(pin)

    monkeypatch.setattr("gobby.cli.runtime.runtime_hub_database", hub)
    _stub_config_projection(monkeypatch)
    _live_daemon(monkeypatch)
    monkeypatch.setattr(
        "gobby.cli.daemon_health.httpx.get",
        lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {"status": "ok"}),
    )

    result = CliRunner().invoke(cli, ["health"])

    assert result.exit_code == 0, result.output
    assert "Schema:" not in result.output


def test_mutating_schema_apply_still_raises_on_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gdaemon-side gate a diverged binary trips must keep failing writes closed."""
    monkeypatch.setattr(schema_contract, "resolve_native_bin", lambda name: "/bin/gdaemon")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["gdaemon"],
            returncode=1,
            stdout="",
            stderr="expected schema identity does not match embedded identity",
        ),
    )

    with pytest.raises(SchemaContractError) as excinfo:
        schema_contract.apply_schema("postgresql://example/db")

    assert "expected schema identity does not match embedded identity" in str(excinfo.value)


def _restart_preflight(
    monkeypatch: pytest.MonkeyPatch,
    *,
    installed: dict[str, object] | None,
    live_head: int,
) -> list[str]:
    """Stage a restart whose stop step records instead of running, and return that record."""
    monkeypatch.setattr(
        schema_divergence,
        "resolve_native_bin",
        lambda name: None if installed is None else "/bin/gdaemon",
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(json.dumps(installed)))

    @contextmanager
    def hub(_config_file: object, *, apply_migrations: bool) -> Iterator[MagicMock]:
        yield _database_returning(live_head)

    monkeypatch.setattr("gobby.cli.runtime.runtime_hub_database", hub)
    _stub_config_projection(monkeypatch)
    monkeypatch.setattr("gobby.cli.daemon.worktree_daemon_refusal", lambda: None)

    stopped: list[str] = []

    def record_stop(*_args: Any, shutdown_intent: str = "stop", **_kwargs: Any) -> bool:
        stopped.append(shutdown_intent)
        return False

    monkeypatch.setattr("gobby.cli.daemon._do_stop", record_stop)
    return stopped


def test_restart_refuses_before_stopping_when_the_installed_binary_is_foreign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equal versions with different checksums is the real case: two branches, one number."""
    expected = schema_contract.expected_schema_identity()
    stopped = _restart_preflight(
        monkeypatch,
        installed={**expected, "latest_checksum": "f" * 64},
        live_head=int(expected["latest_version"]),
    )

    result = CliRunner().invoke(cli, ["restart"])

    assert result.exit_code == 1, result.output
    assert "Refusing to restart" in result.output
    assert "is not this checkout's" in result.output
    assert stopped == [], "the daemon must still be running after a refusal"


def test_restart_refuses_before_stopping_when_the_hub_is_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = schema_contract.expected_schema_identity()
    pin = int(expected["latest_version"])
    stopped = _restart_preflight(monkeypatch, installed=dict(expected), live_head=pin + 4)

    result = CliRunner().invoke(cli, ["restart"])

    assert result.exit_code == 1, result.output
    assert f"live hub schema v{pin + 4} is newer than this checkout (v{pin})" in result.output
    assert stopped == []


def test_restart_proceeds_when_the_hub_merely_owes_a_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary upgrade path: this checkout adds a migration the hub has not applied."""
    expected = schema_contract.expected_schema_identity()
    stopped = _restart_preflight(
        monkeypatch,
        installed=dict(expected),
        live_head=int(expected["latest_version"]) - 1,
    )

    result = CliRunner().invoke(cli, ["restart"])

    assert result.exit_code == 1, result.output
    assert "Refusing to restart" not in result.output
    assert stopped == ["restart"]


def test_restart_proceeds_when_the_installed_identity_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = schema_contract.expected_schema_identity()
    stopped = _restart_preflight(
        monkeypatch, installed=None, live_head=int(expected["latest_version"])
    )

    result = CliRunner().invoke(cli, ["restart"])

    assert result.exit_code == 1, result.output
    assert "Refusing to restart" not in result.output
    assert stopped == ["restart"]


def test_schema_apply_refusal_is_silent_without_a_readable_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = schema_contract.expected_schema_identity()
    monkeypatch.setattr(schema_divergence, "resolve_native_bin", lambda name: "/bin/gdaemon")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(json.dumps(dict(expected))))

    assert schema_divergence.schema_apply_refusal(None) is None
