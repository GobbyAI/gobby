"""Native maintenance indexing keeps the issuing daemon's isolated home identity."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from gobby.code_index.gcode_gateway import GcodeGateway
from gobby.runtime_grants.launch import materialize_managed_launch, merge_child_env
from gobby.runtime_grants.schema import GrantPrincipal, PostgresDirect
from gobby.runtime_grants.service import DeploymentGrantContext, GrantService
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.managed_credentials import ManagedCredentialManager
from tests.runtime_grants.support import (
    DEPLOYMENT_TOKEN,
    FENCING_EPOCH,
    GOLDEN_SECRET,
    StaticRuntime,
    config_snapshot,
    daemon_config,
)
from tests.storage.test_postgres_agent_authorization import RUNTIME_ROLE, AuthorizationFixture

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("merge_env", [False, True], ids=["gateway-override", "sanitized-merge"])
@pytest.mark.parametrize("matching_machine", [True, False], ids=["same-machine", "wrong-machine"])
async def test_native_maintenance_preserves_daemon_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authorization_fixture: AuthorizationFixture,
    request: pytest.FixtureRequest,
    merge_env: bool,
    matching_machine: bool,
) -> None:
    binary = shutil.which("gcode")
    if binary is None:
        pytest.skip("native maintenance regression requires installed gcode")
    fixture = authorization_fixture
    user_home = tmp_path / "user-home"
    home = tmp_path / "daemon-home"
    home.mkdir()
    user_home.mkdir()
    machine_id = fixture.machine_id if matching_machine else fixture.other_machine_id
    (home / "machine_id").write_text(str(machine_id))
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("GOBBY_HOME", str(home))
    for key in (
        "GOBBY_SESSION_ID",
        "GOBBY_AGENT_RUN_ID",
        "GOBBY_MANAGED_EXECUTION_ID",
        "GOBBY_PROJECT_ID",
        "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
        "GOBBY_AGENT_API_TOKEN",
    ):
        monkeypatch.setenv(key, "stale-parent")
    database = PostgresHubDatabase(fixture.database_url, runtime_role=RUNTIME_ROLE)
    database.open()
    manager = ManagedCredentialManager(
        database=database,
        machine_id=fixture.machine_id,
        runtime_root=tmp_path / "credentials",
        owns_database=True,
    )
    request.addfinalizer(manager.close)
    execution_id = uuid4()
    credential = manager.issue_maintenance(
        managed_execution_id=execution_id,
        project_id=fixture.project_id,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    request.addfinalizer(lambda: manager.revoke(execution_id, reason="test-home-complete"))
    now = int(time.time())
    grants = GrantService(
        runtime=StaticRuntime(config_snapshot(daemon_config(), revision=41)),
        context=DeploymentGrantContext(
            token=DEPLOYMENT_TOKEN, fencing_epoch=FENCING_EPOCH, signing_secret=GOLDEN_SECRET
        ),
        clock=lambda: now,
    )
    grant = grants.issue(
        principal=GrantPrincipal(
            kind="maintenance",
            machine_id=str(fixture.machine_id),
            project_id=str(fixture.project_id),
            execution_id=str(execution_id),
            session_id=None,
        ),
        postgres=PostgresDirect(
            dsn=credential.dsn,
            role_name=credential.credential.role_name,
            credential_generation=credential.credential.credential_generation,
            valid_until=int(credential.credential.expires_at.timestamp()),
        ),
        ttl_seconds=120,
    )
    launch = materialize_managed_launch(
        grant, dest_dir=tmp_path / "maintenance", signing_key=b"test-key", deadline_seconds=30
    )
    child_env = merge_child_env(launch.env) if merge_env else launch.env
    assert child_env is not None
    # RED fails before spawn if isolation is missing, protecting the operator home.
    assert child_env.get("GOBBY_HOME") == str(home.resolve())
    assert child_env.get("HOME") == str(user_home.resolve())
    assert "GOBBY_SESSION_ID" not in child_env
    assert "GOBBY_AGENT_RUN_ID" not in child_env
    assert "stale-parent" not in child_env.values()

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gobby").mkdir()
    (root / ".gobby" / "project.json").write_text(json.dumps({"id": str(fixture.project_id)}))
    (root / "sample.py").write_text("def sample():\n    return 42\n")
    admin = PostgresHubDatabase(fixture.database_url)
    request.addfinalizer(admin.close)
    request.addfinalizer(
        lambda: admin.execute(
            "DELETE FROM code_indexed_file_states WHERE project_id = %s", (fixture.project_id,)
        )
    )
    admin.execute(
        "UPDATE project_checkouts SET root_path = %s WHERE machine_id = %s AND project_id = %s",
        (str(root.resolve()), fixture.machine_id, fixture.project_id),
    )
    admin.execute(
        "INSERT INTO deployment_runtime (deployment_token, fencing_epoch, grant_signing_secret) "
        "VALUES (%s, %s, %s) ON CONFLICT (deployment_token) DO NOTHING",
        (DEPLOYMENT_TOKEN, FENCING_EPOCH, GOLDEN_SECRET),
    )
    # No projection sync is requested; this native command needs only the isolated database.
    child_env["GOBBY_DAEMON_URL"] = "http://127.0.0.1:1"
    result = await GcodeGateway(binary=binary).maintenance_index(root, timeout=20, env=child_env)
    if matching_machine:
        assert result.returncode == 0, result.stderr
        indexed = admin.fetchone(
            "SELECT file_path FROM code_indexed_files WHERE project_id = %s AND file_path = %s",
            (fixture.project_id, "sample.py"),
        )
        assert indexed is not None
        assert indexed["file_path"] == "sample.py"
    else:
        assert result.returncode == 2
        assert "grant machine does not match local machine" in result.stderr
