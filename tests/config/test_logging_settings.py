from pathlib import Path

import pytest
from pydantic import ValidationError

from gobby.config._loading import _migrate_legacy_config
from gobby.config.app import DaemonConfig, load_config
from gobby.config.logging import LoggingSettings, resolved_log_path, resolved_logs_dir
from gobby.telemetry.config import TelemetrySettings

LEGACY_TEST_LOG_ENV_VARS = (
    "GOBBY_LOGGING_CLIENT",
    "GOBBY_LOGGING_CLIENT_ERROR",
    "GOBBY_LOGGING_CLIENT_STDERR",
    "GOBBY_LOGGING_MCP_SERVER",
    "GOBBY_LOGGING_MCP_CLIENT",
    "GOBBY_LOGGING_HOOK_MANAGER",
)


def test_logging_settings_defaults_and_resolved_paths() -> None:
    settings = LoggingSettings()

    assert settings.level == "info"
    assert settings.format == "text"
    assert settings.dir == "~/.gobby/logs"
    assert settings.max_size_mb == 10
    assert settings.backup_count == 5
    assert settings.runtime_max_size_mb == 50
    assert settings.growth_warn_mb_per_interval == 100
    assert resolved_logs_dir(settings) == Path("~/.gobby/logs").expanduser()
    assert resolved_log_path(settings, "ui.log") == Path("~/.gobby/logs/ui.log").expanduser()


@pytest.mark.parametrize(
    "field",
    (
        "max_size_mb",
        "backup_count",
        "runtime_max_size_mb",
        "growth_warn_mb_per_interval",
    ),
)
def test_logging_settings_reject_non_positive_sizes(field: str) -> None:
    with pytest.raises(ValidationError, match="Value must be positive"):
        LoggingSettings(**{field: 0})


def test_daemon_config_separates_logging_from_telemetry() -> None:
    config = DaemonConfig()

    assert isinstance(config.logging, LoggingSettings)
    assert isinstance(config.telemetry, TelemetrySettings)
    assert "log_file" not in TelemetrySettings.model_fields
    assert "log_level" not in TelemetrySettings.model_fields


def test_migrate_logging_prefers_explicit_new_fields() -> None:
    migrated = _migrate_legacy_config(
        {
            "logging": {
                "dir": "/new/logs",
                "level": "warning",
                "client": "/old/logs/client.log",
            },
            "telemetry": {
                "log_file": "/legacy/logs/gobby.log",
                "log_level": "debug",
                "max_size_mb": 99,
            },
        }
    )

    assert migrated["logging"] == {
        "dir": "/new/logs",
        "level": "warning",
        "max_size_mb": 99,
    }
    assert migrated["telemetry"] == {}


def test_migrate_logging_derives_directory_and_renames_telemetry_fields() -> None:
    migrated = _migrate_legacy_config(
        {
            "telemetry": {
                "log_file": "/var/log/gobby/gobby.log",
                "log_file_error": "/var/log/gobby/error.log",
                "log_level": "debug",
                "log_format": "json",
                "max_size_mb": 20,
                "backup_count": 7,
                "stderr_log_max_mb": 80,
                "logs_growth_warn_mb_per_interval": 120,
            }
        }
    )

    assert migrated["logging"] == {
        "dir": "/var/log/gobby",
        "level": "debug",
        "format": "json",
        "max_size_mb": 20,
        "backup_count": 7,
        "runtime_max_size_mb": 80,
        "growth_warn_mb_per_interval": 120,
    }
    assert migrated["telemetry"] == {}


def test_migrate_logging_derives_directory_from_legacy_logging_paths() -> None:
    migrated = _migrate_legacy_config(
        {
            "logging": {
                "client_error": "/tmp/gobby/error.log",
                "hook_manager": "/tmp/gobby/hooks.log",
            }
        }
    )

    assert migrated["logging"] == {"dir": "/tmp/gobby"}


def test_migrate_logging_rejects_conflicting_legacy_path_parents() -> None:
    with pytest.raises(
        ValueError,
        match=r"telemetry\.log_file_error.*logging\.client",
    ):
        _migrate_legacy_config(
            {
                "logging": {"client": "/other/gobby.log"},
                "telemetry": {
                    "log_file": "/var/log/gobby/gobby.log",
                    "log_file_error": "/tmp/error.log",
                },
            }
        )


def test_test_protection_uses_gobby_logging_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in LEGACY_TEST_LOG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    logs_dir = tmp_path / "safe-logs"
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.setenv("GOBBY_LOGGING_DIR", str(logs_dir))

    config = load_config(str(tmp_path / "bootstrap.yaml"))

    assert config.logging.dir == str(logs_dir)


def test_test_protection_rejects_conflicting_legacy_env_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in LEGACY_TEST_LOG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("GOBBY_LOGGING_DIR", raising=False)
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.setenv("GOBBY_LOGGING_CLIENT", str(tmp_path / "one" / "gobby.log"))
    monkeypatch.setenv(
        "GOBBY_LOGGING_CLIENT_ERROR",
        str(tmp_path / "two" / "errors.log"),
    )

    with pytest.raises(
        ValueError,
        match=r"GOBBY_LOGGING_CLIENT.*GOBBY_LOGGING_CLIENT_ERROR",
    ):
        load_config(str(tmp_path / "bootstrap.yaml"))
