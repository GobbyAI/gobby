from pathlib import Path

import pytest
from pydantic import ValidationError

from gobby.config.app import DaemonConfig
from gobby.config.logging import (
    LoggingSettings,
    allow_audit_backup_count,
    resolved_log_path,
    resolved_logs_dir,
)
from gobby.telemetry.config import TelemetrySettings


def test_logging_settings_defaults_and_resolved_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOBBY_HOME", raising=False)
    settings = LoggingSettings()

    assert settings.level == "info"
    assert settings.format == "text"
    assert settings.dir == "~/.gobby/logs"
    assert settings.max_size_mb == 10
    assert settings.backup_count == 5
    assert settings.llm_max_size_mb == 50
    assert settings.llm_backup_count == 5
    assert settings.runtime_max_size_mb == 50
    assert settings.growth_warn_mb_per_interval == 100
    assert settings.allow_audit_retention_days == 14
    assert settings.allow_audit_max_size_mb == 256
    assert settings.allow_audit_queue_capacity == 8192
    assert settings.allow_audit_shutdown_timeout_seconds == 2.0
    assert allow_audit_backup_count(settings) == 9
    assert resolved_logs_dir(settings) == Path("~/.gobby/logs").expanduser()
    assert resolved_log_path(settings, "ui.log") == Path("~/.gobby/logs/ui.log").expanduser()


def test_default_logging_paths_follow_gobby_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    isolated_home = tmp_path / "isolated-gobby"
    monkeypatch.setenv("GOBBY_HOME", str(isolated_home))

    settings = LoggingSettings()

    assert resolved_logs_dir(settings) == isolated_home / "logs"
    assert resolved_log_path(settings, "ui.log") == isolated_home / "logs" / "ui.log"


@pytest.mark.parametrize(
    "field",
    (
        "max_size_mb",
        "backup_count",
        "llm_max_size_mb",
        "llm_backup_count",
        "runtime_max_size_mb",
        "growth_warn_mb_per_interval",
        "allow_audit_retention_days",
        "allow_audit_max_size_mb",
        "allow_audit_queue_capacity",
    ),
)
def test_logging_settings_reject_non_positive_sizes(field: str) -> None:
    with pytest.raises(ValidationError):
        LoggingSettings(**{field: 0})


def test_allow_audit_retention_requires_at_least_fourteen_days() -> None:
    with pytest.raises(ValidationError):
        LoggingSettings(allow_audit_retention_days=13)


def test_allow_audit_shutdown_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        LoggingSettings(allow_audit_shutdown_timeout_seconds=0)


def test_daemon_config_separates_logging_from_telemetry() -> None:
    config = DaemonConfig()

    assert isinstance(config.logging, LoggingSettings)
    assert isinstance(config.telemetry, TelemetrySettings)
    assert "log_file" not in TelemetrySettings.model_fields
    assert "log_level" not in TelemetrySettings.model_fields
