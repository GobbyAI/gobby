"""Observability must survive a checkout, a binary, and a hub that disagree."""

from __future__ import annotations

import ast
import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.cli.runtime import CliRuntime
from gobby.config.app import DaemonConfig
from gobby.storage import schema_divergence
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
    source = Path("src/gobby/cli/daemon.py").read_text(encoding="utf-8")

    accessors = _config_accessors_by_function(source)

    for command in ("status", "health", "_do_stop"):
        assert accessors[command] == {"read_only_operational_config"}, command


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
        ("src/gobby/cli/account_identity_cutover.py", "verify_schema"),
    ],
)
def test_fail_closed_paths_still_call_the_identity_gate(relative_path: str, expected: str) -> None:
    source = Path(relative_path).read_text(encoding="utf-8")

    assert expected in source, relative_path
