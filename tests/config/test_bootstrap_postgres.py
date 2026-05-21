"""Bootstrap parsing tests for PostgreSQL backend selection."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.config.fake_keyring import (
    DATABASE_URL_KEY,
    DATABASE_URL_REF,
    KEYRING_SERVICE,
    FakeKeyring,
    install_fake_keyring,
)

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


def test_bootstrap_migrates_plaintext_postgres_url_to_keyring(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.config.bootstrap import load_bootstrap

    fake_keyring = FakeKeyring()
    install_fake_keyring(monkeypatch, fake_keyring)
    bootstrap_file = temp_dir / "bootstrap.yaml"
    database_url = "postgresql://gobby:secret@localhost:60891/gobby"
    _write_bootstrap(
        bootstrap_file,
        "hub_backend: postgres\n"
        f"database_url: {database_url}\n"
        "postgres_install_mode: docker\n"
        "database_path: /tmp/sqlite.db\n",
    )

    bootstrap = load_bootstrap(str(bootstrap_file))

    assert bootstrap.hub_backend == "postgres"
    assert bootstrap.database_url == database_url
    assert bootstrap.postgres_install_mode == "docker"
    assert bootstrap.database_path == "/tmp/sqlite.db"
    assert fake_keyring.set_calls == [(KEYRING_SERVICE, DATABASE_URL_KEY, database_url)]

    persisted = yaml.safe_load(bootstrap_file.read_text())
    assert "database_url" not in persisted
    assert persisted["database_url_ref"] == DATABASE_URL_REF
    assert bootstrap_file.stat().st_mode & 0o777 == 0o600


def test_bootstrap_loads_postgres_url_from_keyring_ref(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.config.bootstrap import load_bootstrap

    database_url = "postgresql://gobby:secret@localhost:60891/gobby"
    fake_keyring = FakeKeyring({(KEYRING_SERVICE, DATABASE_URL_KEY): database_url})
    install_fake_keyring(monkeypatch, fake_keyring)
    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "hub_backend: postgres\n"
        f"database_url_ref: {DATABASE_URL_REF}\n"
        "postgres_install_mode: docker\n",
    )

    bootstrap = load_bootstrap(str(bootstrap_file))

    assert bootstrap.hub_backend == "postgres"
    assert bootstrap.database_url == database_url
    assert bootstrap.postgres_install_mode == "docker"
    assert fake_keyring.get_calls == [(KEYRING_SERVICE, DATABASE_URL_KEY)]
    assert "database_url" not in yaml.safe_load(bootstrap_file.read_text())


def test_write_postgres_defaults_stores_database_url_ref(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.config.postgres_bootstrap import read_bootstrap_database_url, write_postgres_defaults

    database_url = "postgresql://gobby:secret@localhost:60891/gobby"
    fake_keyring = FakeKeyring()
    install_fake_keyring(monkeypatch, fake_keyring)

    write_postgres_defaults(
        gobby_home=temp_dir,
        mode="docker",
        database_url=database_url,
    )

    bootstrap_file = temp_dir / "bootstrap.yaml"
    persisted = yaml.safe_load(bootstrap_file.read_text())
    assert persisted["hub_backend"] == "sqlite"
    assert "database_url" not in persisted
    assert persisted["database_url_ref"] == DATABASE_URL_REF
    assert persisted["postgres_install_mode"] == "docker"
    assert fake_keyring.set_calls == [(KEYRING_SERVICE, DATABASE_URL_KEY, database_url)]
    assert read_bootstrap_database_url(temp_dir) == database_url


def test_postgres_keyring_ref_requires_stored_database_url(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    fake_keyring = FakeKeyring()
    install_fake_keyring(monkeypatch, fake_keyring)
    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_file,
        "hub_backend: postgres\n"
        f"database_url_ref: {DATABASE_URL_REF}\n"
        "postgres_install_mode: docker\n",
    )

    with pytest.raises(BootstrapConfigError, match="keyring.*postgres_database_url"):
        load_bootstrap(str(bootstrap_file))

    assert fake_keyring.get_calls == [(KEYRING_SERVICE, DATABASE_URL_KEY)]


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
