"""Isolated-daemon boundary suite for plan 6.1."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import psycopg
import pytest
from psycopg import sql

from gobby.deployment import deployment_token
from gobby.runtime_grants.handshake import encode_grant_header
from gobby.runtime_grants.launch import write_grant_file
from gobby.runtime_grants.schema import FalkorDirect, GrantBundle, PostgresDirect, QdrantDirect
from gobby.runtime_grants.signing import payload_checksum
from gobby.storage.managed_credentials import ManagedCredentialManager
from gobby.storage.secrets import SecretStore
from gobby.wiki.codewiki_dormant import CODEWIKI_DISABLED_REASON
from tests.e2e.conftest import (
    DaemonInstance,
    authenticated_daemon_client,
    daemon_auth_headers,
    daemon_health_unavailable,
    find_free_port,
    wait_for_daemon_health,
)

pytestmark = pytest.mark.e2e


_HEARTBEAT_SQL = """
CREATE OR REPLACE FUNCTION gobby_agent_auth.heartbeat_daemon(
    p_machine_id UUID,
    p_lease_duration INTERVAL DEFAULT INTERVAL '2 minutes'
) RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, gobby_agent_auth
AS $function$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF p_lease_duration <= INTERVAL '0 seconds'
       OR p_lease_duration > INTERVAL '5 minutes' THEN
        RAISE EXCEPTION 'daemon lease duration must be between zero and five minutes'
            USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.machines WHERE id = p_machine_id) THEN
        RAISE EXCEPTION 'unknown issuing machine' USING ERRCODE = '23503';
    END IF;

    INSERT INTO gobby_agent_auth.daemon_registry (
        machine_id, heartbeat_at, lease_expires_at, started_at
    ) VALUES (
        p_machine_id, v_now, v_now + p_lease_duration, v_now
    )
    ON CONFLICT (machine_id) DO UPDATE
    SET heartbeat_at = EXCLUDED.heartbeat_at,
        lease_expires_at = EXCLUDED.lease_expires_at;
    RETURN p_machine_id;
END
$function$;
"""


def _repair_shared_auth_functions(postgres_db: Any) -> None:
    postgres_db.execute(_HEARTBEAT_SQL)
    postgres_db.execute(
        """
        GRANT EXECUTE ON FUNCTION gobby_agent_auth.heartbeat_daemon(UUID, INTERVAL)
        TO gobby_daemon_runtime
        """
    )
    postgres_db.execute("GRANT SELECT ON public.machines TO gobby_agent_issuer")
    postgres_db.execute("GRANT SELECT ON machines TO gobby_agent_issuer")


def _compose_falkor_password() -> str | None:
    env_password = os.environ.get("GOBBY_FALKORDB_PASSWORD")
    if env_password:
        return env_password
    try:
        inspect = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{range .Config.Env}}{{println .}}{{end}}",
                "services-falkordb-1",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if inspect.returncode != 0:
        return None
    for line in inspect.stdout.splitlines():
        if line.startswith("GOBBY_FALKORDB_PASSWORD="):
            value = line.split("=", 1)[1]
            return value or None
    return None


def _seed_identity_rows(postgres_db: Any, machine_id: str, user_id: str) -> None:
    postgres_db.execute(
        """
        INSERT INTO public.machines (id, owner_user_id)
        VALUES (%s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (machine_id, user_id),
    )
    postgres_db.execute(
        """
        INSERT INTO machines (id, owner_user_id)
        VALUES (%s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (machine_id, user_id),
    )


@pytest.fixture
def e2e_pre_daemon_setup(
    monkeypatch: pytest.MonkeyPatch,
    postgres_db: Any,
    postgres_schema: str,
) -> None:
    debug = Path(__file__).resolve().parents[2] / "target" / "debug"
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(debug))
    from gobby.storage.config_mutations import ConfigMutations, ConfigPatch
    from tests.fixtures.postgres import (
        TEST_USER_EMAIL,
        TEST_USER_ID,
        TEST_USER_NAME,
        TEST_USER_PASSWORD_HASH,
    )

    _repair_shared_auth_functions(postgres_db)
    if not postgres_schema.replace("_", "").isalnum():
        raise RuntimeError(f"refusing to GRANT on unexpected schema {postgres_schema!r}")
    postgres_db.execute(f"GRANT USAGE ON SCHEMA {postgres_schema} TO gobby_gcode_capability")
    postgres_db.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
        f"{postgres_schema} TO gobby_gcode_capability"
    )
    postgres_db.execute(
        """
        INSERT INTO public.users (id, email, name, password_hash)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (TEST_USER_ID, TEST_USER_EMAIL, TEST_USER_NAME, TEST_USER_PASSWORD_HASH),
    )
    postgres_db.execute(
        """
        INSERT INTO users (id, email, name, password_hash)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (TEST_USER_ID, TEST_USER_EMAIL, TEST_USER_NAME, TEST_USER_PASSWORD_HASH),
    )
    _seed_identity_rows(postgres_db, E2E_MACHINE_ID, TEST_USER_ID)
    _seed_identity_rows(postgres_db, STANDBY_MACHINE_ID, TEST_USER_ID)
    mutations = ConfigMutations(postgres_db)
    mutations.patch_internal(
        expected_revision=mutations.repository.current_revision(),
        patch=ConfigPatch(values={"code_index.enabled": True}),
        source="e2e-boundary",
    )


E2E_PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
E2E_MACHINE_ID = "21000000-0000-4000-8000-000000000002"
STANDBY_MACHINE_ID = "21000000-0000-4000-8000-000000000099"
SEMANTIC_WARNING = {
    "lane": "semantic",
    "cause": "daemon_unreachable",
    "message": "semantic search degraded: daemon unreachable; lexical and graph results only",
}
DORMANT_STATUS = {
    "enabled": False,
    "state": "disabled",
    "reason": CODEWIKI_DISABLED_REASON,
}
DORMANT_REFRESH = {
    "error": "codewiki_disabled_pending_redesign",
    "reason": CODEWIKI_DISABLED_REASON,
}
_MODALITY_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/api/embeddings"),
    ("POST", "/api/llm/generate"),
    ("POST", "/api/llm/chat/completions"),
    ("POST", "/api/llm/vision/extract"),
    ("POST", "/api/voice/transcribe"),
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _native_bin(name: str) -> Path:
    debug = _repo_root() / "target" / "debug" / name
    if debug.is_file():
        return debug
    from gobby.utils.native_bin import resolve_native_bin

    found = resolve_native_bin(name)
    if found is None:
        pytest.fail(f"{name} binary is not available in target/debug or PATH")
    return Path(found)


def _postgres_generation(grant: GrantBundle) -> int:
    postgres = grant.capabilities.postgres
    generation = getattr(postgres, "credential_generation", None)
    assert isinstance(generation, int)
    return generation


def _json_payload(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for stream in (completed.stdout, completed.stderr):
        text = stream.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            try:
                payload = json.loads(text.splitlines()[-1])
            except json.JSONDecodeError:
                continue
        if isinstance(payload, dict):
            nested = payload.get("result")
            if isinstance(nested, dict) and isinstance(nested.get("payload"), dict):
                return cast(dict[str, Any], nested["payload"])
            return cast(dict[str, Any], payload)
    return {}


def _assert_typed_failure(completed: subprocess.CompletedProcess[str], *codes: str) -> None:
    assert completed.returncode != 0, completed.stdout or completed.stderr
    payload = _json_payload(completed)
    code = payload.get("code") or payload.get("error")
    assert code in codes, (
        f"expected typed {codes}, got {code!r}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    combined = f"{completed.stdout}\n{completed.stderr}".lower()
    assert "standalone" not in combined
    assert "fallback" not in combined


def _rechecksum(grant: GrantBundle) -> GrantBundle:
    return grant.model_copy(update={"payload_checksum": payload_checksum(grant)})


def _interactive_cache_path(home: Path, token: str, project_id: str) -> Path:
    return home / "grants" / token / f"{project_id}.json"


def _binding_path(home: Path, daemon_url: str) -> Path:
    normalized = daemon_url.strip().rstrip("/")
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:16]
    return home / "grants" / "bindings" / f"{digest}.json"


def persist_interactive_grant(home: Path, daemon_url: str, grant: GrantBundle) -> Path:
    path = write_grant_file(
        _interactive_cache_path(home, grant.deployment.token, grant.principal.project_id),
        grant,
    )
    binding = _binding_path(home, daemon_url)
    binding.parent.mkdir(parents=True, exist_ok=True)
    binding.write_text(
        json.dumps(
            {
                "endpoint": daemon_url.strip().rstrip("/"),
                "deployment_token": grant.deployment.token,
            }
        )
    )
    binding.chmod(0o600)
    return path


def _load_cached_grant(path: Path) -> GrantBundle:
    payload = json.loads(path.read_text())
    if isinstance(payload, dict) and isinstance(payload.get("grant"), dict):
        payload = payload["grant"]
    return GrantBundle.model_validate(payload)


def _age_past_half_ttl(grant: GrantBundle, *, now: int | None = None) -> GrantBundle:
    clock = now if now is not None else int(time.time())
    return _rechecksum(
        grant.model_copy(update={"issued_at": clock - 1000, "expires_at": clock + 100})
    )


def _credential_manager(postgres_db: Any, home: Path) -> ManagedCredentialManager:
    return ManagedCredentialManager(
        database=postgres_db,
        machine_id=UUID(E2E_MACHINE_ID),
        runtime_root=home / "managed-e2e",
    )


def _rotate_interactive(postgres_db: Any, home: Path, grant: GrantBundle) -> int:
    session_id = grant.principal.session_id or grant.principal.project_id
    manager = _credential_manager(postgres_db, home)
    store = SecretStore(postgres_db, gobby_home=home)
    manager.remember_interactive_grant_expiry(
        deployment_token=grant.deployment.token,
        project_id=UUID(grant.principal.project_id),
        generation=_postgres_generation(grant),
        expires_at=datetime.fromtimestamp(grant.expires_at, UTC),
    )
    rotated = manager.rotate_interactive(
        deployment_token=grant.deployment.token,
        project_id=UUID(grant.principal.project_id),
        session_id=UUID(session_id),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        secret_store=store,
    )
    assert rotated.credential_generation > _postgres_generation(grant)
    return rotated.credential_generation


def _revoke_interactive(postgres_db: Any, home: Path, grant: GrantBundle) -> None:
    manager = _credential_manager(postgres_db, home)
    outcome = manager.revoke_interactive(
        deployment_token=grant.deployment.token,
        project_id=UUID(grant.principal.project_id),
        generation=_postgres_generation(grant),
        reason="e2e-explicit-revoke",
    )
    assert outcome.completed is True


def _install_direct_datastores(boundary: BoundaryHarness) -> GrantBundle:
    password = _compose_falkor_password()
    assert password is not None, "FalkorDB password is required for direct-capability restore"
    with authenticated_daemon_client(boundary.daemon) as client:
        current = client.get("/api/config/values")
        assert current.status_code == 200, current.text
        patched = client.patch(
            "/api/config/values",
            json={
                "expected_revision": current.json()["revision"],
                "values": {
                    "databases": {
                        "falkordb": {
                            "host": "127.0.0.1",
                            "port": int(os.environ.get("GOBBY_TEST_FALKOR_PORT", "16379")),
                            "password": password,
                        },
                        "qdrant": {
                            "url": os.environ.get("GOBBY_TEST_QDRANT_URL", "http://127.0.0.1:6333"),
                            "api_key": "e2e",
                        },
                    }
                },
            },
        )
    assert patched.status_code == 200, patched.text
    if boundary.daemon.is_alive():
        boundary.daemon.stop()
    boundary.daemon.restart()
    grant = _handshake_grant(boundary.daemon, boundary.project_dir)
    persist_interactive_grant(boundary.home, boundary.daemon.http_url, grant)
    return grant


def _present_embeddings(daemon: DaemonInstance, home: Path, grant: GrantBundle) -> httpx.Response:
    with authenticated_daemon_client(daemon) as client:
        return client.post(
            "/api/embeddings",
            headers={
                **daemon_auth_headers(home),
                "X-Gobby-Runtime-Grant": encode_grant_header(grant),
                "X-Gobby-Machine-Id": grant.principal.machine_id,
                "X-Gobby-Project-Id": grant.principal.project_id,
                "X-Gobby-Session-Id": grant.principal.session_id or "",
            },
            json={"texts": ["hi"]},
        )


def _assert_direct_postgres(grant: GrantBundle) -> None:
    postgres = grant.capabilities.postgres
    assert isinstance(postgres, PostgresDirect)
    with psycopg.connect(postgres.dsn, connect_timeout=5) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)


def _assert_direct_falkor(grant: GrantBundle) -> None:
    falkor = grant.capabilities.falkordb
    assert isinstance(falkor, FalkorDirect)
    with socket.create_connection((falkor.host, falkor.port), timeout=5) as conn:
        conn.sendall(f"AUTH {falkor.password}\r\nPING\r\n".encode())
        reply = conn.recv(256)
    assert b"+PONG" in reply, reply


def _assert_direct_qdrant(grant: GrantBundle) -> None:
    qdrant = grant.capabilities.qdrant
    assert isinstance(qdrant, QdrantDirect)
    response = httpx.get(
        qdrant.url.rstrip("/") + "/readyz",
        headers={"api-key": qdrant.api_key},
        timeout=5.0,
    )
    assert response.status_code == 200, response.text


def _dump_schema(database_url: str, schema: str, dest: Path) -> None:
    completed = subprocess.run(
        [
            "pg_dump",
            "--dbname",
            database_url,
            "-n",
            schema,
            "--data-only",
            "--column-inserts",
            "--no-owner",
            "--no-acl",
            "-f",
            str(dest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert dest.stat().st_size > 0


def _restore_schema(database_url: str, schema: str, dump: Path, postgres_db: Any) -> None:
    if not schema.replace("_", "").isalnum():
        raise RuntimeError(f"refusing to restore unexpected schema {schema!r}")
    tables = postgres_db.fetchall(
        "SELECT tablename FROM pg_tables WHERE schemaname = %s",
        (schema,),
    )
    names = [str(row["tablename"] if isinstance(row, dict) else row[0]) for row in tables]
    if names:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            connection.execute(
                sql.SQL("TRUNCATE {} CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier(schema, name) for name in names)
                )
            )
    completed = subprocess.run(
        [
            "psql",
            database_url,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f"SET search_path TO {schema}",
            "-f",
            str(dump),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def _spawn_same_deployment_standby(
    boundary: BoundaryHarness,
    *,
    database_url: str,
) -> DaemonInstance:
    http_port = find_free_port()
    ws_port = find_free_port()
    work = boundary.project_dir / f".standby-{http_port}"
    logs = work / "logs"
    logs.mkdir(parents=True)
    bootstrap = work / "bootstrap.yaml"
    bootstrap.write_text(
        "\n".join(
            (
                "hub_backend: postgres",
                f"database_url: {database_url}",
                f"daemon_port: {http_port}",
                "bind_host: localhost",
                f"websocket_port: {ws_port}",
                "",
            )
        )
    )
    bootstrap.chmod(0o600)
    env = dict(boundary.daemon.env)
    env["GOBBY_HOME"] = str(boundary.home)
    env["HOME"] = str(boundary.home)
    env.pop("GOBBY_CONFIG", None)
    script = (
        "import asyncio, sys\n"
        "from pathlib import Path\n"
        "from gobby.runner import run_gobby\n"
        "from gobby.runner_pid_file import FailOpenPidOwnership\n"
        "asyncio.run(run_gobby(\n"
        "    Path(sys.argv[1]),\n"
        "    ownership_resolution=FailOpenPidOwnership('e2e-same-deployment-standby'),\n"
        "))\n"
    )
    log_file = logs / "daemon.log"
    error_log_file = logs / "daemon_error.log"
    command = [sys.executable, "-c", script, str(bootstrap)]
    with log_file.open("wb") as log_handle, error_log_file.open("wb") as error_handle:
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=error_handle,
            stdin=subprocess.DEVNULL,
            cwd=boundary.project_dir,
            env=env,
            start_new_session=True,
        )
    return DaemonInstance(
        process=process,
        pid=process.pid,
        http_port=http_port,
        ws_port=ws_port,
        project_dir=boundary.project_dir,
        gobby_dir=boundary.project_dir / ".gobby",
        log_file=log_file,
        error_log_file=error_log_file,
        db_path=work / "hub-postgres.db",
        config_path=bootstrap,
        command=command,
        env=env,
    )


@dataclass
class BoundaryHarness:
    daemon: DaemonInstance
    project_dir: Path
    grant: GrantBundle
    grant_path: Path

    @property
    def home(self) -> Path:
        return self.daemon.gobby_home

    @property
    def client(self) -> httpx.Client:
        return authenticated_daemon_client(self.daemon)

    def command_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GOBBY_TEST_PROTECT"] = "1"
        env["HOME"] = str(self.home)
        env["GOBBY_HOME"] = str(self.home)
        env["GOBBY_DAEMON_URL"] = self.daemon.http_url
        env.pop("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", None)
        env["PATH"] = f"{_repo_root() / 'target' / 'debug'}:{env.get('PATH', '')}"
        for key in (
            "GCODE_DATABASE_URL",
            "GWIKI_DATABASE_URL",
            "GOBBY_POSTGRES_DSN",
            "GOBBY_FALKORDB_HOST",
            "GOBBY_FALKORDB_PORT",
            "GOBBY_FALKORDB_PASSWORD",
            "GOBBY_QDRANT_URL",
            "GOBBY_QDRANT_API_KEY",
            "GOBBY_RUNTIME_MODE",
        ):
            env.pop(key, None)
        return env

    def run(
        self, binary: str, *args: str, timeout: float = 30.0
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(_native_bin(binary)), "--format", "json", *args],
            cwd=self.project_dir,
            env=self.command_env(),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    def grant_headers(self) -> dict[str, str]:
        return {
            **daemon_auth_headers(self.home),
            "X-Gobby-Runtime-Grant": encode_grant_header(self.grant),
            "X-Gobby-Machine-Id": self.grant.principal.machine_id,
            "X-Gobby-Caller-Project-Id": self.grant.principal.project_id,
            "X-Gobby-Project-Id": self.grant.principal.project_id,
            "X-Gobby-Session-Id": self.grant.principal.session_id or "",
        }


def _handshake_grant(daemon: DaemonInstance, project_dir: Path) -> GrantBundle:
    session_id = str(uuid.uuid4())
    with authenticated_daemon_client(daemon) as client:
        response = client.post(
            "/api/runtime/handshake",
            json={
                "machine_id": E2E_MACHINE_ID,
                "project_id": E2E_PROJECT_ID,
                "session_id": session_id,
            },
        )
    assert response.status_code == 200, (
        f"{response.text}\n--- daemon log ---\n{daemon.read_logs()}\n"
        f"--- daemon error ---\n{daemon.read_error_logs()}"
    )
    payload = response.json()["grant"]
    grant = GrantBundle.model_validate(payload)
    assert grant.principal.project_id == E2E_PROJECT_ID
    assert grant.principal.machine_id == E2E_MACHINE_ID
    _ = project_dir
    return grant


@pytest.fixture
def boundary(daemon_instance: DaemonInstance, e2e_project_dir: Path) -> Iterator[BoundaryHarness]:
    grant = _handshake_grant(daemon_instance, e2e_project_dir)
    grant_path = persist_interactive_grant(
        daemon_instance.gobby_home, daemon_instance.http_url, grant
    )
    yield BoundaryHarness(
        daemon=daemon_instance,
        project_dir=e2e_project_dir,
        grant=grant,
        grant_path=grant_path,
    )


def test_runtime_boundary_scenarios(boundary: BoundaryHarness) -> None:
    search = boundary.run("gcode", "--allow-stale", "search", "fixture")
    assert search.returncode == 0, search.stderr or search.stdout
    wiki_read = boundary.run("gwiki", "status", "--topic", "rust")
    assert wiki_read.returncode == 0, wiki_read.stderr or wiki_read.stdout

    boundary.daemon.stop()
    assert daemon_health_unavailable(boundary.daemon.http_port)

    offline_search = boundary.run("gcode", "--allow-stale", "search", "fixture")
    assert offline_search.returncode == 0, offline_search.stderr or offline_search.stdout
    offline_wiki = boundary.run("gwiki", "status", "--topic", "rust")
    assert offline_wiki.returncode == 0, offline_wiki.stderr or offline_wiki.stdout
    # Explicit AI stays on daemon modality routes; do not pin gwiki ask
    # (#19672 retires that verb). A stopped daemon cannot serve embeddings.
    with pytest.raises(httpx.ConnectError):
        with authenticated_daemon_client(boundary.daemon) as client:
            client.post(
                "/api/embeddings",
                headers=boundary.grant_headers(),
                json={"texts": ["hi"]},
            )
    listed = boundary.run("gcode", "projects")
    _assert_typed_failure(listed, "daemon_required")

    expired = _rechecksum(boundary.grant.model_copy(update={"expires_at": int(time.time()) - 5}))
    write_grant_file(boundary.grant_path, expired)
    expired_cmd = boundary.run("gcode", "--allow-stale", "search", "fixture")
    _assert_typed_failure(expired_cmd, "daemon_required", "expired")
    status = boundary.run("gwiki", "status", "--topic", "rust")
    assert status.returncode == 0, status.stderr or status.stdout
    status_payload = _json_payload(status)
    assert status_payload.get("grant", {}).get("state") in {"expired", "absent", "malformed"}

    write_grant_file(boundary.grant_path, boundary.grant)
    boundary.daemon.restart()
    assert wait_for_daemon_health(boundary.daemon.http_port)
    restarted = boundary.run("gcode", "--allow-stale", "search", "fixture")
    assert restarted.returncode == 0, restarted.stderr or restarted.stdout
    with authenticated_daemon_client(boundary.daemon) as client:
        stale = client.post(
            "/api/embeddings",
            headers=boundary.grant_headers(),
            json={"texts": ["hi"]},
        )
    assert stale.status_code in {401, 403, 409}, stale.text
    stale_code = stale.json().get("code") or stale.json().get("error")
    assert stale_code in {"stale_epoch", "invalid_signature", "wrong_deployment"}


def test_symbol_summary_regression(boundary: BoundaryHarness, postgres_db: Any) -> None:
    source = boundary.project_dir / "pkg" / "sample.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def greet(name: str) -> str:\n    return name\n")
    indexed = boundary.run("gcode", "index", "--full", "--quiet")
    assert indexed.returncode == 0, indexed.stderr or indexed.stdout
    row = postgres_db.fetchone(
        """
        SELECT summary FROM code_symbols
        WHERE project_id = %s AND name = %s
        """,
        (E2E_PROJECT_ID, "greet"),
    )
    assert row is not None
    retrieved = boundary.run("gcode", "--allow-stale", "search-symbol", "greet")
    assert retrieved.returncode == 0, retrieved.stderr or retrieved.stdout


def test_modality_identity_binding(boundary: BoundaryHarness) -> None:
    headers = boundary.grant_headers()
    with authenticated_daemon_client(boundary.daemon) as client:
        for method, path in _MODALITY_ROUTES:
            ok = client.request(method, path, headers=headers)
            ok_code = (ok.json().get("code") or ok.json().get("error")) if ok.content else None
            assert ok_code != "forged_identity", (path, ok.text)
            forged = client.request(
                method,
                path,
                headers=headers
                | {
                    "X-Gobby-Machine-Id": "forged-machine",
                    "X-Gobby-Project-Id": "forged-project",
                },
            )
            assert forged.status_code == 401, (path, forged.text)
            assert (forged.json().get("code") or forged.json().get("error")) == "forged_identity"


def test_concurrent_renewal_race(boundary: BoundaryHarness, postgres_db: Any) -> None:
    _ = postgres_db
    predecessor_generation = _postgres_generation(boundary.grant)
    expired = _rechecksum(boundary.grant.model_copy(update={"expires_at": int(time.time()) - 5}))
    write_grant_file(boundary.grant_path, expired)

    processes = [
        subprocess.Popen(
            [
                str(_native_bin("gcode")),
                "--format",
                "json",
                "--allow-stale",
                "search",
                "fixture",
            ],
            cwd=boundary.project_dir,
            env=boundary.command_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=60) for process in processes]
    failures = [
        (process.returncode, stdout, stderr)
        for process, (stdout, stderr) in zip(processes, results, strict=True)
        if process.returncode != 0
    ]
    assert len(failures) < 2, failures
    follow_up = boundary.run("gcode", "--allow-stale", "search", "fixture")
    assert follow_up.returncode == 0, follow_up.stderr or follow_up.stdout

    cached = _load_cached_grant(boundary.grant_path)
    cached_generation = _postgres_generation(cached)
    assert cached.expires_at > expired.expires_at
    assert cached.issued_at >= boundary.grant.issued_at
    assert cached_generation >= predecessor_generation
    search = boundary.run("gcode", "--allow-stale", "search", "fixture")
    assert search.returncode == 0, search.stderr or search.stdout


def test_broker_scope_paths(boundary: BoundaryHarness) -> None:
    listed = boundary.run("gcode", "projects")
    assert listed.returncode == 0, listed.stderr or listed.stdout
    project_prune = boundary.run("gcode", "prune", "--force")
    assert project_prune.returncode in {0, 2}, project_prune.stderr or project_prune.stdout
    with authenticated_daemon_client(boundary.daemon) as client:
        global_prune = client.post("/api/code-index/prune", json={"force": True})
        assert global_prune.status_code in {200, 202, 409}, global_prune.text
        capability = client.post(
            "/api/code-index/prune",
            headers={"Authorization": "Bearer not-an-operator"},
            json={"force": True},
        )
        assert capability.status_code == 401, capability.text


def test_diagnostics_under_expiry(boundary: BoundaryHarness) -> None:
    expired = _rechecksum(boundary.grant.model_copy(update={"expires_at": int(time.time()) - 10}))
    write_grant_file(boundary.grant_path, expired)
    status = boundary.run("gwiki", "status", "--topic", "rust")
    assert status.returncode == 0, status.stderr or status.stdout
    payload = _json_payload(status)
    assert payload.get("grant", {}).get("state") in {"expired", "absent", "malformed"}
    hidden = boundary.grant_path.with_suffix(".json.hidden")
    boundary.grant_path.rename(hidden)
    try:
        completed = subprocess.run(
            [str(_native_bin("gwiki")), "--format", "json", "status", "--topic", "rust"],
            cwd=boundary.project_dir,
            env=boundary.command_env(),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    finally:
        hidden.rename(boundary.grant_path)
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_search_degrades_with_warning(boundary: BoundaryHarness) -> None:
    boundary.daemon.stop()
    search = boundary.run("gcode", "--allow-stale", "search", "fixture")
    assert search.returncode == 0, search.stderr or search.stdout
    payload = _json_payload(search)
    warnings = payload.get("warnings") or []
    assert SEMANTIC_WARNING in warnings
    listed = boundary.run("gcode", "projects")
    _assert_typed_failure(listed, "daemon_required")


def test_dormant_codewiki_unchanged(boundary: BoundaryHarness) -> None:
    headers = boundary.grant_headers()
    with authenticated_daemon_client(boundary.daemon) as client:
        status = client.get("/api/wiki/code/status", headers=headers)
        refresh = client.post("/api/wiki/code/refresh", headers=headers)
    assert status.status_code == 200
    assert status.json() == DORMANT_STATUS
    assert refresh.status_code == 409
    assert refresh.json() == DORMANT_REFRESH


def test_restore_replay_rejected(
    boundary: BoundaryHarness,
    postgres_db: Any,
    postgres_database_url: str,
    postgres_schema: str,
    tmp_path: Path,
) -> None:
    archived = _install_direct_datastores(boundary)
    boundary.grant = archived
    boundary.grant_path = persist_interactive_grant(
        boundary.home, boundary.daemon.http_url, archived
    )
    assert isinstance(archived.capabilities.postgres, PostgresDirect)
    assert isinstance(archived.capabilities.falkordb, FalkorDirect)
    assert isinstance(archived.capabilities.qdrant, QdrantDirect)
    dump = tmp_path / "hub-schema.sql"
    _dump_schema(postgres_database_url, postgres_schema, dump)
    if boundary.daemon.is_alive():
        boundary.daemon.stop()
    _restore_schema(postgres_database_url, postgres_schema, dump, postgres_db)
    _repair_shared_auth_functions(postgres_db)
    boundary.daemon.restart()
    assert wait_for_daemon_health(boundary.daemon.http_port)
    presented = _present_embeddings(boundary.daemon, boundary.home, archived)
    assert presented.status_code in {401, 403, 409}, presented.text
    presented_code = presented.json().get("code") or presented.json().get("error")
    assert presented_code in {"invalid_signature", "stale_epoch"}, presented.text
    _assert_direct_postgres(archived)
    _assert_direct_falkor(archived)
    _assert_direct_qdrant(archived)
    write_grant_file(boundary.grant_path, archived)
    boundary.daemon.stop()
    offline = boundary.run("gcode", "--allow-stale", "search", "fixture")
    assert offline.returncode == 0, offline.stderr or offline.stdout
    expired = _rechecksum(archived.model_copy(update={"expires_at": int(time.time()) - 5}))
    write_grant_file(boundary.grant_path, expired)
    expired_offline = boundary.run("gcode", "--allow-stale", "search", "fixture")
    _assert_typed_failure(expired_offline, "expired", "daemon_required")


def test_takeover_fencing(
    boundary: BoundaryHarness,
    postgres_database_url: str,
    postgres_schema: str,
    postgres_db: Any,
) -> None:
    from tests.e2e.conftest import _postgres_url_for_schema, terminate_process_tree

    token = deployment_token(boundary.home)
    assert token == boundary.grant.deployment.token
    with authenticated_daemon_client(boundary.daemon) as client:
        current = client.get("/api/config/values")
        assert current.status_code == 200, current.text
        patched = client.patch(
            "/api/config/values",
            json={
                "expected_revision": current.json()["revision"],
                "values": {
                    "databases": {
                        "qdrant": {
                            "url": os.environ.get("GOBBY_TEST_QDRANT_URL", "http://127.0.0.1:6333"),
                            "api_key": "e2e",
                        }
                    }
                },
            },
        )
    assert patched.status_code == 200, patched.text
    qdrant_grant = _handshake_grant(boundary.daemon, boundary.project_dir)
    if qdrant_grant.capabilities.qdrant.mode == "unavailable":
        if boundary.daemon.is_alive():
            boundary.daemon.stop()
        boundary.daemon.restart()
        qdrant_grant = _handshake_grant(boundary.daemon, boundary.project_dir)
    persist_interactive_grant(boundary.home, boundary.daemon.http_url, qdrant_grant)
    boundary.grant = qdrant_grant
    boundary.grant_path = persist_interactive_grant(
        boundary.home, boundary.daemon.http_url, qdrant_grant
    )
    assert qdrant_grant.capabilities.qdrant.mode != "unavailable", qdrant_grant.capabilities.qdrant
    postgres_db.execute(
        """
        INSERT INTO code_indexed_projects (id) VALUES (%s)
        ON CONFLICT (id) DO NOTHING
        """,
        (E2E_PROJECT_ID,),
    )
    postgres_db.execute(
        """
        INSERT INTO code_indexed_project_states (
            machine_id, project_id, root_path
        ) VALUES (%s, %s, %s)
        ON CONFLICT (machine_id, project_id) DO UPDATE
           SET root_path = EXCLUDED.root_path
        """,
        (E2E_MACHINE_ID, E2E_PROJECT_ID, str(boundary.project_dir)),
    )
    postgres_db.execute(
        """
        INSERT INTO code_index_prune_dirty_projects (
            machine_id, project_id, root_path, reason
        ) VALUES (%s, %s, %s, %s)
        ON CONFLICT (machine_id, project_id) DO UPDATE
           SET reason = EXCLUDED.reason
        """,
        (E2E_MACHINE_ID, E2E_PROJECT_ID, str(boundary.project_dir), "e2e-takeover"),
    )
    scoped_url = _postgres_url_for_schema(postgres_database_url, postgres_schema)
    standby = _spawn_same_deployment_standby(boundary, database_url=scoped_url)
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if standby.process.poll() is not None:
                pytest.fail(
                    "same-deployment standby exited\n"
                    f"--- log ---\n{standby.read_logs()}\n"
                    f"--- error ---\n{standby.read_error_logs()}"
                )
            try:
                health = httpx.get(f"{standby.http_url}/api/admin/health", timeout=1.0)
            except httpx.HTTPError:
                time.sleep(0.1)
                continue
            if health.status_code == 200 and health.json().get("lease_mode") == "standby":
                break
        else:
            pytest.fail(
                "same-deployment standby did not report lease_mode=standby\n"
                f"--- log ---\n{standby.read_logs()}\n"
                f"--- error ---\n{standby.read_error_logs()}"
            )

        admitted: dict[str, int | None] = {"status": None}
        barrier = threading.Event()

        def _in_flight_prune() -> None:
            barrier.wait(timeout=10)
            with httpx.Client(base_url=boundary.daemon.http_url, timeout=20.0) as client:
                response = client.post(
                    "/api/code-index/invalidate",
                    headers=boundary.grant_headers(),
                    json={"project_id": E2E_PROJECT_ID},
                )
            admitted["status"] = response.status_code

        worker = threading.Thread(target=_in_flight_prune, daemon=True)
        worker.start()
        with psycopg.connect(postgres_db.conninfo, autocommit=False) as lock_conn:
            lock_conn.execute(
                """
                SELECT fencing_epoch
                  FROM deployment_runtime
                 WHERE deployment_token = %s
                 FOR UPDATE
                """,
                (token,),
            )
            barrier.set()
            waiting = False
            wait_deadline = time.monotonic() + 10.0
            while time.monotonic() < wait_deadline:
                if admitted["status"] is not None:
                    break
                row = lock_conn.execute(
                    """
                    SELECT COUNT(*) AS waiting
                      FROM pg_stat_activity
                     WHERE pid <> pg_backend_pid()
                       AND wait_event_type = 'Lock'
                    """
                ).fetchone()
                if row is None:
                    time.sleep(0.05)
                    continue
                waiting_count = row["waiting"] if isinstance(row, dict) else row[0]
                if int(waiting_count) > 0:
                    waiting = True
                    break
                time.sleep(0.05)
            assert waiting or admitted["status"] is None, (
                "in-flight handler finished before the epoch fence "
                f"status={admitted['status']} qdrant={boundary.grant.capabilities.qdrant.mode}"
            )
            lock_conn.execute(
                """
                UPDATE deployment_runtime
                   SET fencing_epoch = fencing_epoch + 1,
                       grant_signing_secret = %s,
                       epoch_updated_at = clock_timestamp()
                 WHERE deployment_token = %s
                """,
                ("e2e-takeover-rotated-secret", token),
            )
            lock_conn.commit()
        worker.join(timeout=20)
        assert worker.is_alive() is False
        remaining = postgres_db.fetchone(
            """
            SELECT root_path FROM code_indexed_project_states
             WHERE machine_id = %s AND project_id = %s
            """,
            (E2E_MACHINE_ID, E2E_PROJECT_ID),
        )
        assert remaining is not None
        assert remaining["root_path"] == str(boundary.project_dir)
        assert admitted["status"] in {409, 500}, admitted

        headers = daemon_auth_headers(boundary.home)
        recover = httpx.post(
            f"{standby.http_url}/api/admin/lease/recover",
            headers=headers,
            params={"stale_after_seconds": 0},
            timeout=20.0,
        )
        assert recover.status_code == 200, recover.text
        assert wait_for_daemon_health(standby.http_port, timeout=30.0), (
            f"promoted owner failed to serve\n--- log ---\n{standby.read_logs()}\n"
            f"--- error ---\n{standby.read_error_logs()}"
        )
        with httpx.Client(base_url=boundary.daemon.http_url, timeout=5.0) as displaced:
            refused = displaced.post(
                "/api/code-index/invalidate",
                headers=boundary.grant_headers(),
                json={"project_id": E2E_PROJECT_ID},
            )
        assert refused.status_code == 409, refused.text
        with httpx.Client(base_url=standby.http_url, timeout=5.0) as owner:
            served = owner.get("/api/auth/status", headers=headers)
        assert served.status_code == 200, served.text
    finally:
        if standby.is_alive():
            terminate_process_tree(standby.pid)


def test_rotation_drain_and_revocation(boundary: BoundaryHarness, postgres_db: Any) -> None:
    first = boundary.grant
    postgres = first.capabilities.postgres
    assert isinstance(postgres, PostgresDirect)
    rotated_generation = _rotate_interactive(postgres_db, boundary.home, first)
    write_grant_file(boundary.grant_path, first)
    boundary.daemon.stop()
    _assert_direct_postgres(first)
    drained = boundary.run("gcode", "--allow-stale", "search", "fixture")
    assert drained.returncode == 0, drained.stderr or drained.stdout

    boundary.daemon.restart()
    assert wait_for_daemon_health(boundary.daemon.http_port)
    live = _handshake_grant(boundary.daemon, boundary.project_dir)
    assert _postgres_generation(live) == rotated_generation
    _revoke_interactive(postgres_db, boundary.home, live)
    revoked = _present_embeddings(boundary.daemon, boundary.home, live)
    revoked_code = revoked.json().get("code") or revoked.json().get("error")
    assert revoked_code == "revoked", revoked.text

    successor = _handshake_grant(boundary.daemon, boundary.project_dir)
    write_grant_file(boundary.grant_path, successor)
    successor_postgres = successor.capabilities.postgres
    assert isinstance(successor_postgres, PostgresDirect)
    boundary.daemon.stop()
    with psycopg.connect(postgres_db.conninfo, autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s",
            (successor_postgres.role_name,),
        )
        admin.execute(
            sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(successor_postgres.role_name))
        )
    invalidated = boundary.run("gcode", "--allow-stale", "search", "fixture")
    assert invalidated.returncode != 0, invalidated.stdout or invalidated.stderr
    payload = _json_payload(invalidated)
    code = payload.get("code") or payload.get("error")
    assert code != "revoked"
    combined = f"{invalidated.stdout}\n{invalidated.stderr}\n{code}".lower()
    assert any(
        needle in combined
        for needle in (
            "authoriz",
            "password",
            "authentication",
            "permission",
            "login",
            "log in",
            "not permitted",
            "failed to connect",
            "fatal",
        )
    ), (code, invalidated.stdout, invalidated.stderr)
