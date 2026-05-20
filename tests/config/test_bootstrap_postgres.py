"""Bootstrap parsing tests for PostgreSQL backend selection."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _write_bootstrap(path: Path, content: str, mode: int = 0o600) -> None:
    path.write_text(content)
    path.chmod(mode)


def test_bootstrap_defaults_to_sqlite_backend(temp_dir: Path) -> None:
    from gobby.config.bootstrap import load_bootstrap

    bootstrap = load_bootstrap(str(temp_dir / "missing.yaml"))

    assert bootstrap.hub_backend == "sqlite"
    assert bootstrap.database_url is None
    assert bootstrap.postgres_install_mode is None


def test_bootstrap_loads_postgres_fields_from_yaml(temp_dir: Path) -> None:
    from gobby.config.bootstrap import load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "hub_backend: postgres\n"
        "database_url: postgresql://gobby:secret@localhost:60891/gobby\n"
        "postgres_install_mode: docker\n"
        "database_path: /tmp/sqlite.db\n",
    )

    bootstrap = load_bootstrap(str(bootstrap_file))

    assert bootstrap.hub_backend == "postgres"
    assert bootstrap.database_url == "postgresql://gobby:secret@localhost:60891/gobby"
    assert bootstrap.postgres_install_mode == "docker"
    assert bootstrap.database_path == "/tmp/sqlite.db"


def test_postgres_backend_requires_database_url(temp_dir: Path) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(bootstrap_file, "hub_backend: postgres\n")

    with pytest.raises(BootstrapConfigError, match="database_url"):
        load_bootstrap(str(bootstrap_file))


@pytest.mark.parametrize(
    ("content", "expected_message"),
    [
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
    _write_bootstrap(bootstrap_file, "hub_backend: sqlite\n", mode=0o644)

    with pytest.raises(BootstrapConfigError, match="permissions.*0600"):
        load_bootstrap(str(bootstrap_file))
