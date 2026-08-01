"""Bootstrap parsing tests for PostgreSQL backend selection."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def _write_bootstrap(path: Path, content: str, mode: int = 0o600) -> None:
    path.write_text(content)
    path.chmod(mode)


def test_bootstrap_defaults_without_runtime_url(temp_dir: Path) -> None:
    from gobby.config.bootstrap import load_bootstrap

    bootstrap = load_bootstrap(str(temp_dir / "missing.yaml"))

    assert bootstrap.database_url is None
    assert bootstrap.postgres_pool.min_size == 2
    assert bootstrap.postgres_pool.max_size == 20
    assert bootstrap.postgres_pool.acquire_timeout_seconds == 5.0
    assert bootstrap.postgres_pool.open_timeout_seconds == 30.0


def test_bootstrap_loads_postgres_pool_settings(temp_dir: Path) -> None:
    from gobby.config.bootstrap import load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "postgres_pool:\n"
        "  min_size: 4\n"
        "  max_size: 24\n"
        "  acquire_timeout_seconds: 7.5\n"
        "  open_timeout_seconds: 12.5\n",
    )

    pool = load_bootstrap(str(bootstrap_file)).postgres_pool

    assert pool.min_size == 4
    assert pool.max_size == 24
    assert pool.acquire_timeout_seconds == 7.5
    assert pool.open_timeout_seconds == 12.5


@pytest.mark.parametrize(
    ("pool_yaml", "expected_message"),
    [
        ("min_size: 0", "min_size must be a positive integer"),
        ("max_size: -1", "max_size must be a positive integer"),
        ("acquire_timeout_seconds: 0", "acquire_timeout_seconds.*positive"),
        ("open_timeout_seconds: -1", "open_timeout_seconds.*positive"),
        ("min_size: 21\nmax_size: 20", "min_size must be less than or equal"),
    ],
)
def test_bootstrap_rejects_invalid_postgres_pool_settings(
    temp_dir: Path,
    pool_yaml: str,
    expected_message: str,
) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    indented_yaml = pool_yaml.replace("\n", "\n  ")
    _write_bootstrap(bootstrap_file, f"postgres_pool:\n  {indented_yaml}\n")

    with pytest.raises(BootstrapConfigError, match=expected_message):
        load_bootstrap(str(bootstrap_file))


def test_bootstrap_loads_postgres_database_url(temp_dir: Path) -> None:
    from gobby.config.bootstrap import load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    database_url = "postgresql://gobby:secret@localhost:60891/gobby"
    _write_bootstrap(
        bootstrap_file,
        f"database_url: {database_url}\n",
    )

    bootstrap = load_bootstrap(str(bootstrap_file), resolve_database_url=True)

    assert bootstrap.database_url == database_url

    persisted = yaml.safe_load(bootstrap_file.read_text())
    assert persisted["database_url"] == database_url
    assert "database_url_ref" not in persisted
    assert bootstrap_file.stat().st_mode & 0o777 == 0o600


def test_write_postgres_defaults_stores_database_url(temp_dir: Path) -> None:
    from gobby.config.postgres_bootstrap import read_bootstrap_database_url, write_postgres_defaults

    database_url = "postgresql://gobby:secret@localhost:60891/gobby"

    write_postgres_defaults(
        gobby_home=temp_dir,
        database_url=database_url,
    )

    bootstrap_file = temp_dir / "bootstrap.yaml"
    persisted = yaml.safe_load(bootstrap_file.read_text())
    assert "hub_backend" not in persisted
    assert "database_path" not in persisted
    assert persisted["database_url"] == database_url
    assert "database_url_ref" not in persisted
    assert "postgres_install_mode" not in persisted
    assert persisted["postgres_pool"] == {
        "min_size": 2,
        "max_size": 20,
        "acquire_timeout_seconds": 5.0,
        "open_timeout_seconds": 30.0,
    }
    assert read_bootstrap_database_url(temp_dir) == database_url


def test_postgres_defaults_follow_runtime_gobby_home_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from gobby.config.postgres_bootstrap import bootstrap_path, write_postgres_defaults

    first_home = tmp_path / "first-home"
    second_home = tmp_path / "second-home"
    database_url = "postgresql://gobby:secret@localhost:60891/gobby"

    for gobby_home in (first_home, second_home):
        monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
        resolved_home = bootstrap_path().parent
        write_postgres_defaults(
            gobby_home=resolved_home,
            database_url=database_url,
        )

        persisted = yaml.safe_load((gobby_home / "bootstrap.yaml").read_text())
        assert "hub_backend" not in persisted
        assert "database_path" not in persisted
        assert persisted["postgres_pool"] == {
            "min_size": 2,
            "max_size": 20,
            "acquire_timeout_seconds": 5.0,
            "open_timeout_seconds": 30.0,
        }


def test_load_bootstrap_without_resolution_reads_plain_database_url(temp_dir: Path) -> None:
    from gobby.config.bootstrap import load_bootstrap

    database_url = "postgresql://gobby:secret@localhost:60891/gobby"
    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        f"database_url: {database_url}\ndaemon_port: 61234\n",
    )

    bootstrap = load_bootstrap(str(bootstrap_file))

    assert bootstrap.daemon_port == 61234
    assert bootstrap.database_url == database_url


def test_load_bootstrap_rejects_removed_postgres_install_mode(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "database_url: postgresql://gobby:secret@localhost:60891/gobby\n"
        "postgres_install_mode: bogus\n",
    )

    with pytest.raises(BootstrapConfigError, match="postgres_install_mode has been removed"):
        load_bootstrap(str(bootstrap_file))


def test_write_postgres_defaults_refreshes_database_url(temp_dir: Path) -> None:
    from gobby.config.postgres_bootstrap import read_bootstrap_database_url, write_postgres_defaults

    first_database_url = "postgresql://gobby:first@localhost:60891/gobby"
    second_database_url = "postgresql://gobby:second@localhost:60891/gobby"

    write_postgres_defaults(
        gobby_home=temp_dir,
        database_url=first_database_url,
    )
    assert read_bootstrap_database_url(temp_dir) == first_database_url

    write_postgres_defaults(
        gobby_home=temp_dir,
        database_url=second_database_url,
    )

    assert read_bootstrap_database_url(temp_dir) == second_database_url


def test_clear_postgres_fields_preserves_postgres_runtime_bootstrap(temp_dir: Path) -> None:
    from gobby.config.postgres_bootstrap import clear_postgres_fields

    bootstrap_file = temp_dir / "bootstrap.yaml"
    database_url = "postgresql://gobby:secret@localhost:60891/gobby"
    _write_bootstrap(
        bootstrap_file,
        f"database_url: {database_url}\npostgres_install_mode: docker\n",
    )

    clear_postgres_fields(temp_dir)

    persisted = yaml.safe_load(bootstrap_file.read_text())
    assert "hub_backend" not in persisted
    assert "database_path" not in persisted
    assert persisted["database_url"] == database_url
    assert "postgres_install_mode" not in persisted


def test_clear_postgres_fields_rejects_unsupported_database_url_ref(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError
    from gobby.config.postgres_bootstrap import clear_postgres_fields

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "database_url_ref: unsupported:gobby:postgres_database_url\n",
    )

    with pytest.raises(BootstrapConfigError, match="requires database_url"):
        clear_postgres_fields(temp_dir)

    persisted = yaml.safe_load(bootstrap_file.read_text())
    assert "hub_backend" not in persisted
    assert persisted["database_url_ref"] == "unsupported:gobby:postgres_database_url"


def test_clear_postgres_fields_removes_legacy_bootstrap_keys(temp_dir: Path) -> None:
    from gobby.config.postgres_bootstrap import clear_postgres_fields

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "hub_backend: local\n"
        "database_path: /legacy/gobby.db\n"
        "database_url: postgresql://gobby:secret@localhost:60891/gobby\n",
    )

    clear_postgres_fields(temp_dir)

    persisted = yaml.safe_load(bootstrap_file.read_text())
    assert "hub_backend" not in persisted
    assert "database_path" not in persisted
    assert persisted["database_url"] == "postgresql://gobby:secret@localhost:60891/gobby"


def test_database_url_ref_is_rejected_for_runtime(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "database_url_ref: unsupported:gobby:postgres_database_url\n",
    )

    with pytest.raises(BootstrapConfigError, match="database_url_ref is no longer supported"):
        load_bootstrap(str(bootstrap_file), resolve_database_url=True)


def test_database_url_ref_is_rejected_for_metadata_only(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "database_url_ref: daemon:gobby:postgres_database_url\ndaemon_port: 61234\n",
    )

    with pytest.raises(BootstrapConfigError, match="database_url_ref is no longer supported"):
        load_bootstrap(str(bootstrap_file))


def test_runtime_requires_database_url(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(bootstrap_file, "auth_mode: required\n")

    with pytest.raises(BootstrapConfigError, match="database_url"):
        load_bootstrap(str(bootstrap_file), resolve_database_url=True)


def test_default_runtime_backend_requires_database_url(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(bootstrap_file, "daemon_port: 60887\n")

    with pytest.raises(BootstrapConfigError, match="database_url"):
        load_bootstrap(str(bootstrap_file), resolve_database_url=True)


def test_runtime_bootstrap_rejects_external_postgres_url(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "database_url: postgresql://gobby:secret@db.example.com:60891/gobby\n",
    )

    with pytest.raises(BootstrapConfigError, match="local Docker-managed PostgreSQL"):
        load_bootstrap(str(bootstrap_file), resolve_database_url=True)


def test_runtime_bootstrap_requires_managed_config_file(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    with pytest.raises(BootstrapConfigError, match="database_url is required"):
        load_bootstrap(str(temp_dir / "missing.yaml"), resolve_database_url=True)


@pytest.mark.parametrize(
    ("content", "expected_message"),
    [
        ("database_url: 123\n", "database_url"),
        ("postgres_install_mode: managed\n", "postgres_install_mode"),
    ],
)
def test_invalid_postgres_bootstrap_modes_raise_field_level_error(
    temp_dir: Path,
    content: str,
    expected_message: str,
) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(bootstrap_file, content)

    with pytest.raises(BootstrapConfigError, match=expected_message):
        load_bootstrap(str(bootstrap_file))


def test_bootstrap_rejects_insecure_file_permissions(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(bootstrap_file, "auth_mode: required\n", mode=0o644)

    with pytest.raises(BootstrapConfigError, match="permissions.*0600"):
        load_bootstrap(str(bootstrap_file))
