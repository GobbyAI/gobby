"""Check child environment isolation without importing or starting the daemon."""

import json
import os
from pathlib import Path
from typing import NoReturn

import prepare_daemon
import prepare_identity
import pytest
from prepare_daemon import isolated_environment, known_config_values, record_preparation

import gobby


def test_child_clears_managed_identity_and_shared_datastores(tmp_path: Path) -> None:
    ambient = {
        "GOBBY_HOME": "/live",
        "GOBBY_SESSION_ID": "live-session",
        "GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/live/grant.json",
        "GOBBY_NATIVE_BIN_DIR": "/live/bin",
        "DATABASE_URL": "live-database",
        "OPENAI_API_KEY": "private",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "live-telemetry",
        "HOME": "/Users/operator",
        "CODEX_HOME": "/Users/operator/.codex",
        "PATH": "/bin",
    }
    child = isolated_environment(tmp_path, ambient)
    assert child["GOBBY_HOME"] == str(tmp_path / "gobby-home")
    assert child["GOBBY_NATIVE_BIN_DIR"] == str(tmp_path / "gobby-home/bin")
    assert child["GOBBY_TEST_PROTECT"] == "1"
    assert not {
        "DATABASE_URL",
        "GOBBY_SESSION_ID",
        "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
        "OPENAI_API_KEY",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    }.intersection(child)
    assert child["HOME"] == ambient["HOME"]
    assert child["CODEX_HOME"] == ambient["CODEX_HOME"]
    assert child["PATH"].split(":") == [str(tmp_path / "tools/gobby/venv/bin"), "/usr/bin", "/bin"]
    assert ambient["GOBBY_HOME"] == "/live"


def test_preparation_receipts_preserve_each_revision_and_resume(tmp_path: Path) -> None:
    record_preparation(tmp_path, 1, [])
    first = (tmp_path / "receipts/daemon-preparation-rev-1.json").read_bytes()
    record_preparation(tmp_path, 4, ["retired.key"])
    record_preparation(tmp_path, 4, ["retired.key"])
    assert (tmp_path / "receipts/daemon-preparation-rev-1.json").read_bytes() == first
    latest = json.loads((tmp_path / "receipts/daemon-preparation-rev-4.json").read_text())
    assert latest["config_revision"] == 4
    assert latest["gobby_home"] == str(tmp_path / "gobby-home")
    assert latest["automatic_indexing"] is False
    assert latest["dropped_config_keys"] == ["retired.key"]


def test_known_config_values_drops_unregistered_keys() -> None:
    kept, dropped = known_config_values({"test_mode": True, "zz_retired_bakeoff_key": 1})
    assert kept == {"test_mode": True}
    assert dropped == ["zz_retired_bakeoff_key"]


class _ReachedDatabase(Exception):
    """Stops preparation at its first database call, after the identity check has passed."""


def _stop_at_database(*_args: object) -> NoReturn:
    raise _ReachedDatabase


def _runtime_root(tmp_path: Path) -> Path:
    """A runtime root whose interpreter stands in for the gobby-sync environment."""
    root = tmp_path / "root"
    (root / "sources/gobby").mkdir(parents=True)
    interpreter = root / "tools/gobby/venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    report = json.dumps(
        {
            "gobby": str(root / "sources/gobby/src/gobby/__init__.py"),
            "gdaemon": str(root / "gobby-home/bin/gdaemon"),
        }
    )
    # The stand-in records the path it was executed by, then prints its own gobby identity.
    interpreter.write_text(
        "#!/bin/sh\n"
        f"printf '%s' \"$0\" > '{root / 'interpreter-used.txt'}'\n"
        f"printf '%s\\n' '{report}'\n"
    )
    interpreter.chmod(0o755)
    return root


def _services_env(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    (root / "config/services.env").write_text(
        "BAKEOFF_POSTGRES_USER=gobby_bakeoff\n"
        "BAKEOFF_POSTGRES_PASSWORD=bakeoff-secret\n"
        "BAKEOFF_POSTGRES_DB=gobby_bakeoff_22948\n"
        "BAKEOFF_FALKORDB_PASSWORD=falkor-secret\n"
    )


def _bootstrap(root: Path) -> None:
    (root / "gobby-home").mkdir(parents=True)
    (root / "gobby-home/bootstrap.yaml").write_text(
        "database_url: postgresql://gobby_bakeoff:bakeoff-secret@127.0.0.1:61234/gobby_bakeoff_22948\n"
    )


def test_runtime_identity_is_probed_by_the_runtime_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime_root(tmp_path)
    # The condition under test: this process's own gobby resolves outside the runtime root.
    assert not Path(gobby.__file__).resolve().is_relative_to(root.resolve())
    _services_env(root)
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    monkeypatch.setattr(prepare_daemon, "preserve_image_probe", _stop_at_database)

    with pytest.raises(_ReachedDatabase):
        prepare_daemon.prepare(root)

    assert (root / "interpreter-used.txt").read_text() == str(root / "tools/gobby/venv/bin/python")


def test_account_identity_is_probed_by_the_runtime_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime_root(tmp_path)
    assert not Path(gobby.__file__).resolve().is_relative_to(root.resolve())
    _bootstrap(root)
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    monkeypatch.setattr("gobby.storage.hub.postgres.PostgresHubDatabase", _stop_at_database)

    with pytest.raises(_ReachedDatabase):
        prepare_identity.prepare_identity(root)

    assert (root / "interpreter-used.txt").read_text() == str(root / "tools/gobby/venv/bin/python")
