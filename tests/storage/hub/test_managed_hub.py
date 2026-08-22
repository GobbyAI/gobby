"""Managed executions open the hub through their grant, never the operator bootstrap."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from gobby.config.bootstrap import BootstrapConfigError
from gobby.config.postgres_pool import DEFAULT_POSTGRES_POOL_CONFIG
from gobby.storage.hub.managed import managed_grant_path, managed_hub_database
from gobby.storage.hub.runtime import runtime_hub_database
from gobby.storage.managed_credentials import MANAGED_EXECUTION_BOOTSTRAP_ENV

pytestmark = pytest.mark.unit

_GOLDEN = Path(__file__).resolve().parents[2] / "runtime_grants" / "golden"
_DIRECT = _GOLDEN / "direct_datastores.json"
_BEFORE_EXPIRY = 1700000001


def _write_grant(tmp_path: Path, source: Path = _DIRECT, **overrides: object) -> Path:
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload.update(overrides)
    grant_path = tmp_path / "grant.json"
    grant_path.write_text(json.dumps(payload), encoding="utf-8")
    return grant_path


def test_managed_grant_path_requires_a_non_blank_value() -> None:
    assert managed_grant_path({}) is None
    assert managed_grant_path({MANAGED_EXECUTION_BOOTSTRAP_ENV: "  "}) is None
    assert managed_grant_path({MANAGED_EXECUTION_BOOTSTRAP_ENV: "/run/grant.json"}) == Path(
        "/run/grant.json"
    )


def test_direct_grant_opens_the_scoped_dsn_without_a_runtime_role(tmp_path: Path) -> None:
    grant_path = _write_grant(tmp_path)
    grant = json.loads(_DIRECT.read_text(encoding="utf-8"))

    with patch("gobby.storage.hub.postgres.PostgresHubDatabase") as database_class:
        database_class.return_value = MagicMock()
        result = managed_hub_database(grant_path, now=_BEFORE_EXPIRY)

    assert result is database_class.return_value
    assert database_class.call_args_list == [
        call(grant["capabilities"]["postgres"]["dsn"], pool_config=DEFAULT_POSTGRES_POOL_CONFIG)
    ]


@pytest.mark.parametrize("source", ["brokered_datastores.json", "unavailable_datastores.json"])
def test_non_direct_postgres_capability_is_refused(tmp_path: Path, source: str) -> None:
    grant_path = _write_grant(tmp_path, _GOLDEN / source)

    with pytest.raises(BootstrapConfigError, match="no direct PostgreSQL capability"):
        managed_hub_database(grant_path, now=_BEFORE_EXPIRY)


def test_expired_grant_is_refused(tmp_path: Path) -> None:
    grant_path = _write_grant(tmp_path)
    grant = json.loads(_DIRECT.read_text(encoding="utf-8"))

    with pytest.raises(BootstrapConfigError, match="expired"):
        managed_hub_database(grant_path, now=grant["expires_at"])


def test_expired_postgres_credential_is_refused(tmp_path: Path) -> None:
    grant = json.loads(_DIRECT.read_text(encoding="utf-8"))
    grant["capabilities"]["postgres"]["valid_until"] = _BEFORE_EXPIRY
    grant_path = _write_grant(tmp_path, capabilities=grant["capabilities"])

    with pytest.raises(BootstrapConfigError, match="expired"):
        managed_hub_database(grant_path, now=_BEFORE_EXPIRY)


def test_unreadable_and_malformed_grants_are_bootstrap_errors(tmp_path: Path) -> None:
    with pytest.raises(BootstrapConfigError, match="cannot read managed execution grant"):
        managed_hub_database(tmp_path / "missing.json", now=_BEFORE_EXPIRY)

    malformed = _write_grant(tmp_path, unexpected="field")
    with pytest.raises(BootstrapConfigError, match="malformed"):
        managed_hub_database(malformed, now=_BEFORE_EXPIRY)


def test_runtime_opener_uses_the_grant_and_skips_operator_bootstrap_and_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant_path = _write_grant(tmp_path)
    monkeypatch.setenv(MANAGED_EXECUTION_BOOTSTRAP_ENV, str(grant_path))

    with (
        patch("gobby.storage.hub.runtime.load_bootstrap") as load_bootstrap,
        patch("gobby.storage.hub.managed.time.time", return_value=_BEFORE_EXPIRY),
        patch("gobby.storage.hub.postgres.PostgresHubDatabase") as database_class,
    ):
        database_class.return_value = MagicMock()
        with runtime_hub_database(str(tmp_path / "bootstrap.yaml")) as db:
            assert db is database_class.return_value

    assert load_bootstrap.call_args_list == []
    database = database_class.return_value
    assert database.apply_migrations.call_args_list == []
    assert database.close.call_args_list == [call()]


def test_runtime_opener_keeps_the_operator_path_outside_managed_executions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MANAGED_EXECUTION_BOOTSTRAP_ENV, raising=False)

    with (
        patch(
            "gobby.storage.hub.runtime.load_bootstrap",
            side_effect=BootstrapConfigError("operator bootstrap consulted"),
        ),
        pytest.raises(BootstrapConfigError, match="operator bootstrap consulted"),
        runtime_hub_database(apply_migrations=False),
    ):
        pass
