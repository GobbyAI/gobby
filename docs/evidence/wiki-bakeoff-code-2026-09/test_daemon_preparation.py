"""Check child environment isolation without importing or starting the daemon."""

import json
from pathlib import Path

from prepare_daemon import isolated_environment, record_preparation


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
    record_preparation(tmp_path, 1)
    first = (tmp_path / "receipts/daemon-preparation-rev-1.json").read_bytes()
    record_preparation(tmp_path, 4)
    record_preparation(tmp_path, 4)
    assert (tmp_path / "receipts/daemon-preparation-rev-1.json").read_bytes() == first
    latest = json.loads((tmp_path / "receipts/daemon-preparation-rev-4.json").read_text())
    assert latest["config_revision"] == 4
    assert latest["gobby_home"] == str(tmp_path / "gobby-home")
    assert latest["automatic_indexing"] is False
