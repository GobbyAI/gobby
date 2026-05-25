"""Bootstrap parsing tests for PostgreSQL backend selection."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def _write_bootstrap(path: Path, content: str, mode: int = 0o600) -> None:
    path.write_text(content)
    path.chmod(mode)


def test_bootstrap_defaults_to_postgres_backend_without_runtime_url(temp_dir: Path) -> None:
    from gobby.config.bootstrap import load_bootstrap

    bootstrap = load_bootstrap(str(temp_dir / "missing.yaml"))

    assert bootstrap.hub_backend == "postgres"
    assert bootstrap.database_url is None
    assert bootstrap.postgres_install_mode is None


def test_bootstrap_loads_postgres_database_url(temp_dir: Path) -> None:
    from gobby.config.bootstrap import load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    database_url = "postgresql://gobby:secret@localhost:60891/gobby"
    _write_bootstrap(
        bootstrap_file,
        f"hub_backend: postgres\ndatabase_url: {database_url}\npostgres_install_mode: docker\n",
    )

    bootstrap = load_bootstrap(str(bootstrap_file), resolve_database_url=True)

    assert bootstrap.hub_backend == "postgres"
    assert bootstrap.database_url == database_url
    assert bootstrap.postgres_install_mode == "docker"

    persisted = yaml.safe_load(bootstrap_file.read_text())
    assert persisted["database_url"] == database_url
    assert "database_url_ref" not in persisted
    assert bootstrap_file.stat().st_mode & 0o777 == 0o600


def test_write_postgres_defaults_stores_database_url(temp_dir: Path) -> None:
    from gobby.config.postgres_bootstrap import read_bootstrap_database_url, write_postgres_defaults

    database_url = "postgresql://gobby:secret@localhost:60891/gobby"

    write_postgres_defaults(
        gobby_home=temp_dir,
        mode="docker",
        database_url=database_url,
    )

    bootstrap_file = temp_dir / "bootstrap.yaml"
    persisted = yaml.safe_load(bootstrap_file.read_text())
    assert persisted["hub_backend"] == "postgres"
    assert persisted["database_url"] == database_url
    assert "database_url_ref" not in persisted
    assert persisted["postgres_install_mode"] == "docker"
    assert read_bootstrap_database_url(temp_dir) == database_url


def test_load_bootstrap_without_resolution_reads_plain_database_url(temp_dir: Path) -> None:
    from gobby.config.bootstrap import load_bootstrap

    database_url = "postgresql://gobby:secret@localhost:60891/gobby"
    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "hub_backend: postgres\n"
        f"database_url: {database_url}\n"
        "postgres_install_mode: docker\n"
        "daemon_port: 61234\n",
    )

    bootstrap = load_bootstrap(str(bootstrap_file))

    assert bootstrap.hub_backend == "postgres"
    assert bootstrap.daemon_port == 61234
    assert bootstrap.database_url == database_url


def test_write_postgres_defaults_refreshes_database_url(temp_dir: Path) -> None:
    from gobby.config.postgres_bootstrap import read_bootstrap_database_url, write_postgres_defaults

    first_database_url = "postgresql://gobby:first@localhost:60891/gobby"
    second_database_url = "postgresql://gobby:second@localhost:60891/gobby"

    write_postgres_defaults(
        gobby_home=temp_dir,
        mode="docker",
        database_url=first_database_url,
    )
    assert read_bootstrap_database_url(temp_dir) == first_database_url

    write_postgres_defaults(
        gobby_home=temp_dir,
        mode="docker",
        database_url=second_database_url,
    )

    assert read_bootstrap_database_url(temp_dir) == second_database_url


def test_clear_postgres_fields_preserves_postgres_runtime_bootstrap(temp_dir: Path) -> None:
    from gobby.config.postgres_bootstrap import clear_postgres_fields

    bootstrap_file = temp_dir / "bootstrap.yaml"
    database_url = "postgresql://gobby:secret@localhost:60891/gobby"
    _write_bootstrap(
        bootstrap_file,
        f"hub_backend: postgres\ndatabase_url: {database_url}\npostgres_install_mode: docker\n",
    )

    clear_postgres_fields(temp_dir)

    persisted = yaml.safe_load(bootstrap_file.read_text())
    assert persisted["hub_backend"] == "postgres"
    assert persisted["database_url"] == database_url
    assert persisted["postgres_install_mode"] == "docker"


def test_clear_postgres_fields_rejects_legacy_database_url_ref(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError
    from gobby.config.postgres_bootstrap import clear_postgres_fields

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "hub_backend: postgres\ndatabase_url_ref: keyring:gobby:postgres_database_url\n"
        "postgres_install_mode: docker\n",
    )

    with pytest.raises(BootstrapConfigError, match="requires database_url"):
        clear_postgres_fields(temp_dir)

    persisted = yaml.safe_load(bootstrap_file.read_text())
    assert persisted["hub_backend"] == "postgres"
    assert persisted["database_url_ref"] == "keyring:gobby:postgres_database_url"
    assert persisted["postgres_install_mode"] == "docker"


def test_clear_postgres_fields_rejects_invalid_runtime_backend(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError
    from gobby.config.postgres_bootstrap import clear_postgres_fields

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "hub_backend: local\n"
        "database_url: postgresql://gobby:secret@localhost:60891/gobby\n"
        "postgres_install_mode: docker\n",
    )

    with pytest.raises(BootstrapConfigError, match="requires hub_backend=postgres"):
        clear_postgres_fields(temp_dir)

    persisted = yaml.safe_load(bootstrap_file.read_text())
    assert persisted["hub_backend"] == "local"
    assert persisted["database_url"] == "postgresql://gobby:secret@localhost:60891/gobby"


def test_database_url_ref_is_rejected_for_runtime(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "hub_backend: postgres\n"
        "database_url_ref: keyring:gobby:postgres_database_url\n"
        "postgres_install_mode: docker\n",
    )

    with pytest.raises(BootstrapConfigError, match="database_url_ref is no longer supported"):
        load_bootstrap(str(bootstrap_file), resolve_database_url=True)


def test_database_url_ref_is_rejected_for_metadata_only(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "hub_backend: postgres\n"
        "database_url_ref: daemon:gobby:postgres_database_url\n"
        "daemon_port: 61234\n",
    )

    with pytest.raises(BootstrapConfigError, match="database_url_ref is no longer supported"):
        load_bootstrap(str(bootstrap_file))


def test_postgres_backend_requires_database_url(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(bootstrap_file, "hub_backend: postgres\n")

    with pytest.raises(BootstrapConfigError, match="database_url"):
        load_bootstrap(str(bootstrap_file), resolve_database_url=True)


@pytest.mark.parametrize(
    ("content", "expected_message"),
    [
        ("hub_backend: local\n", "hub_backend"),
        ("hub_backend: mysql\n", "hub_backend"),
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
    _write_bootstrap(bootstrap_file, "hub_backend: postgres\n", mode=0o644)

    with pytest.raises(BootstrapConfigError, match="permissions.*0600"):
        load_bootstrap(str(bootstrap_file))
