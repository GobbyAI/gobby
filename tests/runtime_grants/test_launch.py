"""Managed launch file and isolated child environment."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gobby.runtime_grants import GrantBundle
from gobby.runtime_grants.launch import (
    materialize_managed_launch,
    merge_child_env,
    write_grant_file,
)
from gobby.utils.local_token import AgentApiTokenClaims, daemon_auth_headers, verify_agent_api_token

pytestmark = pytest.mark.unit

_GOLDEN = Path(__file__).resolve().parent / "golden" / "direct_datastores.json"


def _grant() -> GrantBundle:
    return GrantBundle.model_validate_json(_GOLDEN.read_bytes())


def test_managed_launch_publishes_capability_in_existing_private_envelope(
    tmp_path: Path,
) -> None:
    from gobby.runtime_grants.schema import GrantPrincipal

    grant = _grant().model_copy(
        update={
            "principal": GrantPrincipal(
                kind="agent_run",
                machine_id="test-machine",
                project_id="test-project",
                execution_id="test-run",
                session_id="test-session",
            )
        }
    )
    launch = materialize_managed_launch(
        grant, dest_dir=tmp_path, signing_key=b"test-signing-key", deadline_seconds=86400
    )
    envelope = json.loads(launch.grant_path.read_bytes())
    assert "managed_api_token" in envelope, "standing seat has no renewable capability source"
    claims = verify_agent_api_token(envelope["managed_api_token"], b"test-signing-key")
    assert isinstance(claims, AgentApiTokenClaims)
    assert claims.agent_run_id == "test-run"
    assert claims.session_id == "test-session"
    assert stat.S_IMODE(launch.grant_path.stat().st_mode) == 0o600
    assert set(tmp_path.iterdir()) == {launch.grant_path}, "renewal must use the existing file"


def test_rotation_renews_capability_in_existing_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from gobby.runtime_grants.launch import rewrite_managed_grant_file
    from gobby.runtime_grants.schema import GrantPrincipal
    from gobby.runtime_grants.signing import signature_matches
    from gobby.utils import local_token
    from tests.runtime_grants.support import DEPLOYMENT_TOKEN

    now = 1_800_000_000
    monkeypatch.setattr("gobby.utils.local_token.time.time", lambda: now)
    monkeypatch.setattr(local_token, "read_managed_signing_key", lambda: b"test-renewal-key")
    grant = _grant().model_copy(
        update={
            "principal": GrantPrincipal(
                kind="agent_run",
                machine_id="test-machine",
                project_id="test-project",
                execution_id="test-run",
                session_id="test-session",
            )
        }
    )
    launch = materialize_managed_launch(
        grant, dest_dir=tmp_path, signing_key=b"test-renewal-key", deadline_seconds=86400
    )
    original = launch.env["GOBBY_AGENT_API_TOKEN"]
    now += 23 * 3600
    issued = datetime.fromtimestamp(now, UTC)
    rewrite_managed_grant_file(
        launch.grant_path,
        managed_execution_id="test-run",
        scoped_dsn="postgresql://fixture",
        role_name="test-role",
        credential_generation=2,
        issued_at=issued,
        expires_at=issued + timedelta(minutes=59),
        deployment_token=DEPLOYMENT_TOKEN,
        fencing_epoch=1,
        signing_secret="test-grant-signing-secret",
    )
    envelope = json.loads(launch.grant_path.read_bytes())
    assert "managed_api_token" in envelope, "rotation discarded the standing seat capability"
    renewed = envelope["managed_api_token"]
    assert renewed != original, "rotation retained the launch-time capability"
    now += 2 * 3600
    assert verify_agent_api_token(original, b"test-renewal-key") is None
    claims = verify_agent_api_token(renewed, b"test-renewal-key")
    assert isinstance(claims, AgentApiTokenClaims)
    assert claims.iat == int(issued.timestamp())
    assert claims.agent_run_id == "test-run"
    assert signature_matches(GrantBundle.model_validate(envelope), "test-grant-signing-secret")
    assert stat.S_IMODE(launch.grant_path.stat().st_mode) == 0o600
    assert original not in caplog.text and renewed not in caplog.text


def test_python_reader_rereads_envelope_instead_of_launch_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.runtime_grants.schema import GrantPrincipal

    grant = _grant().model_copy(
        update={
            "principal": GrantPrincipal(
                kind="agent_run",
                machine_id="test-machine",
                project_id="test-project",
                execution_id="test-run",
                session_id="test-session",
            )
        }
    )
    launch = materialize_managed_launch(
        grant, dest_dir=tmp_path, signing_key=b"test-reader-key", deadline_seconds=86400
    )
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "test-run")
    monkeypatch.setenv("GOBBY_SESSION_ID", "test-session")
    monkeypatch.setenv("GOBBY_PROJECT_ID", "test-project")
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_ID", raising=False)
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", str(launch.grant_path))
    monkeypatch.setenv("GOBBY_AGENT_API_TOKEN", "launch-time-fixture")
    assert (
        daemon_auth_headers().get("Authorization")
        == "Bearer " + launch.env["GOBBY_AGENT_API_TOKEN"]
    )
    envelope = json.loads(launch.grant_path.read_bytes())
    envelope["managed_api_token"] = "renewed-fixture"
    launch.grant_path.write_text(json.dumps(envelope))
    assert daemon_auth_headers().get("Authorization") == "Bearer renewed-fixture"


def test_merge_child_env_returns_none_for_missing_extra() -> None:
    assert merge_child_env(None) is None


def test_writer_preserves_envelope_capability(tmp_path: Path) -> None:
    grant = _grant().model_copy(update={"managed_api_token": "fixture-current"})
    path = write_grant_file(tmp_path / "grant.json", grant)
    assert json.loads(path.read_bytes())["managed_api_token"] == "fixture-current"


def test_rotation_refuses_nonprivate_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.runtime_grants.launch import rewrite_managed_grant_file
    from gobby.runtime_grants.schema import GrantPrincipal
    from tests.runtime_grants.support import DEPLOYMENT_TOKEN

    monkeypatch.setattr("gobby.utils.local_token.read_managed_signing_key", lambda: b"test-key")
    grant = _grant().model_copy(
        update={
            "managed_api_token": "fixture-current",
            "principal": GrantPrincipal(
                kind="agent_run",
                machine_id="machine",
                project_id="project",
                execution_id="run",
                session_id="seat",
            ),
        }
    )
    path = write_grant_file(tmp_path / "grant.json", grant)
    path.chmod(0o640)
    before = path.read_bytes()
    issued = datetime.now(UTC)
    with pytest.raises(ValueError, match="not private"):
        rewrite_managed_grant_file(
            path,
            managed_execution_id="run",
            scoped_dsn="postgresql://fixture",
            role_name="fixture",
            credential_generation=2,
            issued_at=issued,
            expires_at=issued + timedelta(minutes=59),
            deployment_token=DEPLOYMENT_TOKEN,
            fencing_epoch=1,
            signing_secret="test-signing",
        )
    assert path.read_bytes() == before


def test_managed_source_without_capability_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_grant_file(tmp_path / "grant.json", _grant())
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", str(path))
    monkeypatch.delenv("GOBBY_AGENT_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="managed capability"):
        daemon_auth_headers()


@pytest.mark.parametrize(
    "invalid", ["permissions", "execution", "session", "project", "ambiguous", "json"]
)
def test_managed_capability_reader_refuses_invalid_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    path = tmp_path / "grant.json"
    payload = {
        "managed_api_token": "private-fixture",
        "principal": {
            "execution_id": "run",
            "session_id": "seat",
            "project_id": "project",
        },
    }
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", str(path))
    monkeypatch.setenv("GOBBY_AGENT_API_TOKEN", "stale-fixture")
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "other" if invalid == "execution" else "run")
    monkeypatch.setenv("GOBBY_SESSION_ID", "other" if invalid == "session" else "seat")
    monkeypatch.setenv("GOBBY_PROJECT_ID", "other" if invalid == "project" else "project")
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_ID", raising=False)
    if invalid == "permissions":
        path.chmod(0o640)
    elif invalid == "ambiguous":
        monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_ID", "run")
    elif invalid == "json":
        path.write_text("invalid-json")
    with pytest.raises(RuntimeError, match="managed capability") as error:
        daemon_auth_headers()
    assert "private-fixture" not in str(error.value)


def test_merge_child_env_omits_unsanctioned_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_LEAK", "should-not-copy")
    monkeypatch.setenv("PATH", "/bin")
    extra = {
        "GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/tmp/grant.json",
        "GOBBY_AGENT_API_TOKEN": "tok",
        "OTHER": "nope",
    }

    env = merge_child_env(extra)

    assert env is not None
    assert env["PATH"] == "/bin"
    assert env["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"] == "/tmp/grant.json"
    assert env["GOBBY_AGENT_API_TOKEN"] == "tok"
    assert "SECRET_LEAK" not in env
    assert "OTHER" not in env


def test_write_grant_file_closes_descriptor_if_chmod_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    real_close = os.close

    def boom(_descriptor: int, _mode: int) -> None:
        raise OSError("chmod failed")

    def spy_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "fchmod", boom)
    monkeypatch.setattr(os, "close", spy_close)

    with pytest.raises(OSError, match="chmod failed"):
        write_grant_file(tmp_path / "grant.json", _grant())

    assert closed
    assert not (tmp_path / "grant.json").exists()
