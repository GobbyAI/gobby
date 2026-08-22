"""A managed execution reads the code index through its scoped grant credential."""

from __future__ import annotations

import json
import time
from pathlib import Path

import psycopg
import pytest

from gobby.code_index.storage import CodeIndexStorage
from gobby.storage.hub.runtime import runtime_hub_database
from gobby.storage.managed_credentials import MANAGED_EXECUTION_BOOTSTRAP_ENV
from tests.storage.test_postgres_agent_authorization import AuthorizationFixture

pytestmark = pytest.mark.integration
pytest_plugins = ("tests.storage.test_postgres_agent_authorization",)

_GOLDEN = Path(__file__).resolve().parents[2] / "runtime_grants" / "golden"


def _managed_grant(fixture: AuthorizationFixture, path: Path) -> Path:
    grant = json.loads((_GOLDEN / "direct_datastores.json").read_text(encoding="utf-8"))
    expires_at = int(time.time()) + 1800
    grant["expires_at"] = expires_at
    grant["principal"] = {
        "kind": "agent_run",
        "machine_id": str(fixture.machine_id),
        "project_id": str(fixture.project_id),
        "execution_id": str(fixture.execution_id),
        "session_id": str(fixture.session_id),
    }
    grant["capabilities"]["postgres"] = {
        "mode": "direct",
        "dsn": fixture.agent_url,
        "role_name": fixture.role_name,
        "credential_generation": 1,
        "valid_until": expires_at,
    }
    path.write_text(json.dumps(grant), encoding="utf-8")
    return path


def test_managed_execution_reads_its_project_code_index_and_nothing_else(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = authorization_fixture
    grant_path = _managed_grant(fixture, tmp_path / "grant.json")
    monkeypatch.setenv(MANAGED_EXECUTION_BOOTSTRAP_ENV, str(grant_path))
    monkeypatch.setattr(
        "gobby.code_index._storage.projects.require_machine_id",
        lambda: str(fixture.machine_id),
    )

    with runtime_hub_database(str(tmp_path / "absent-bootstrap.yaml")) as db:
        storage = CodeIndexStorage(db)
        own = storage.get_project_stats(str(fixture.project_id))
        assert own is not None
        assert storage.get_project_stats(str(fixture.other_project_id)) is None
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.fetchone("SELECT id FROM tasks LIMIT 1")
