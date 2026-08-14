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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    authenticated_daemon_client_for_home,
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
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
_HANDSHAKE_PATH = "/api/runtime/handshake"
_COLLECTIONS_PATH = "/collections"


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


@dataclass
class _LoopbackProxy:
    server: ThreadingHTTPServer
    thread: threading.Thread
    url: str

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _start_loopback_proxy(handler: type[BaseHTTPRequestHandler]) -> _LoopbackProxy:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    hostname = host.decode() if isinstance(host, bytes) else str(host)
    return _LoopbackProxy(server=server, thread=thread, url=f"http://{hostname}:{port}")


def _forward_http(upstream: str, handler: BaseHTTPRequestHandler) -> None:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    body = handler.rfile.read(length) if length else b""
    headers = {
        key: value
        for key, value in handler.headers.items()
        if key.lower() not in {"host", "content-length", "transfer-encoding", "connection"}
    }
    with httpx.Client(timeout=15.0) as client:
        response = client.request(
            handler.command,
            upstream.rstrip("/") + handler.path,
            headers=headers,
            content=body,
        )
    payload = response.content
    handler.send_response(response.status_code)
    for key, value in response.headers.items():
        if key.lower() in {"transfer-encoding", "content-encoding", "connection", "content-length"}:
            continue
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _start_handshake_grant_proxy(upstream: str, grants: list[GrantBundle]) -> _LoopbackProxy:
    issued = list(grants)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            del fmt, args

        def do_GET(self) -> None:
            _forward_http(upstream, self)

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != _HANDSHAKE_PATH:
                _forward_http(upstream, self)
                return
            with lock:
                if not issued:
                    self.send_error(500, "no scripted handshake grants remain")
                    return
                grant = issued.pop(0)
            body = json.dumps({"grant": grant.model_dump(mode="json")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _start_loopback_proxy(Handler)


def _start_qdrant_key_proxy(upstream: str, api_key: str) -> _LoopbackProxy:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            del fmt, args

        def _authorized(self) -> bool:
            return self.headers.get("api-key") == api_key

        def do_GET(self) -> None:
            if not self._authorized():
                self.send_error(401, "unauthorized")
                return
            _forward_http(upstream, self)

        def do_PUT(self) -> None:
            self.do_POST()

        def do_POST(self) -> None:
            if not self._authorized():
                self.send_error(401, "unauthorized")
                return
            _forward_http(upstream, self)

        def do_DELETE(self) -> None:
            if not self._authorized():
                self.send_error(401, "unauthorized")
                return
            _forward_http(upstream, self)

    return _start_loopback_proxy(Handler)


def _admit_barrier_paths(home: Path) -> tuple[Path, Path, Path]:
    runtime = home / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    flag = runtime / "e2e-admit-barrier"
    return (
        flag,
        flag.with_name("e2e-admit-barrier.admitted"),
        flag.with_name("e2e-admit-barrier.release"),
    )


def _wait_for_path(path: Path, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


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


def _install_direct_datastores(
    boundary: BoundaryHarness,
    *,
    qdrant_url: str | None = None,
    qdrant_api_key: str = "e2e",
) -> GrantBundle:
    password = _compose_falkor_password()
    assert password is not None, "FalkorDB password is required for direct-capability restore"
    qdrant_endpoint = qdrant_url or os.environ.get("GOBBY_TEST_QDRANT_URL", "http://127.0.0.1:6333")
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
                            "url": qdrant_endpoint,
                            "api_key": qdrant_api_key,
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
    collections = qdrant.url.rstrip("/") + _COLLECTIONS_PATH
    authorized = httpx.get(
        collections,
        headers={"api-key": qdrant.api_key},
        timeout=5.0,
    )
    assert authorized.status_code == 200, authorized.text
    payload = authorized.json()
    assert isinstance(payload, dict)
    rejected = httpx.get(
        collections,
        headers={"api-key": f"wrong-{qdrant.api_key}"},
        timeout=5.0,
    )
    assert rejected.status_code in {401, 403}, rejected.text


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

    def command_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
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
        if extra:
            env.update(extra)
        return env

    def run(
        self,
        binary: str,
        *args: str,
        timeout: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(_native_bin(binary)), "--format", "json", *args],
            cwd=self.project_dir,
            env=env or self.command_env(),
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


def _handshake_grant(
    daemon: DaemonInstance,
    project_dir: Path,
    *,
    home: Path | None = None,
) -> GrantBundle:
    session_id = str(uuid.uuid4())
    auth_home = home or daemon.gobby_home
    with authenticated_daemon_client_for_home(daemon.http_url, auth_home) as client:
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


def _wiki_read(boundary: BoundaryHarness, *args: str) -> subprocess.CompletedProcess[str]:
    return boundary.run("gwiki", *args, "--topic", "rust")


def test_runtime_boundary_scenarios(boundary: BoundaryHarness) -> None:
    search = boundary.run("gcode", "--allow-stale", "search", "fixture")
    assert search.returncode == 0, search.stderr or search.stdout
    initialized = _wiki_read(boundary, "init")
    assert initialized.returncode == 0, initialized.stderr or initialized.stdout
    vault = Path(str(_json_payload(initialized).get("root") or ""))
    assert vault.is_dir(), initialized.stdout
    page = vault / "knowledge" / "concepts" / "e2e.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# E2E\nwiki read fixture\n")
    wiki_read = _wiki_read(boundary, "read", "--path", "knowledge/concepts/e2e.md")
    assert wiki_read.returncode == 0, wiki_read.stderr or wiki_read.stdout
    assert "wiki read fixture" in f"{wiki_read.stdout}\n{wiki_read.stderr}"

    boundary.daemon.stop()
    assert daemon_health_unavailable(boundary.daemon.http_port)

    offline_search = boundary.run("gcode", "--allow-stale", "search", "fixture")
    assert offline_search.returncode == 0, offline_search.stderr or offline_search.stdout
    offline_wiki = _wiki_read(boundary, "read", "--path", "knowledge/concepts/e2e.md")
    assert offline_wiki.returncode == 0, offline_wiki.stderr or offline_wiki.stdout
    assert "wiki read fixture" in f"{offline_wiki.stdout}\n{offline_wiki.stderr}"
    image = boundary.project_dir / "e2e-vision.png"
    image.write_bytes(_TINY_PNG)
    offline_ai = _wiki_read(boundary, "ingest-file", str(image))
    _assert_typed_failure(offline_ai, "daemon_required", "daemon_error", "config_error")
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
        SELECT id, summary FROM code_symbols
        WHERE project_id = %s AND name = %s
        """,
        (E2E_PROJECT_ID, "greet"),
    )
    assert row is not None
    stored = row["summary"] if isinstance(row, dict) else row[1]
    if not stored:
        stored = "returns the provided name"
        symbol_id = row["id"] if isinstance(row, dict) else row[0]
        postgres_db.execute(
            "UPDATE code_symbols SET summary = %s WHERE id = %s",
            (stored, symbol_id),
        )
    assert isinstance(stored, str) and stored.strip()
    retrieved = boundary.run("gcode", "--allow-stale", "search-symbol", "greet")
    assert retrieved.returncode == 0, retrieved.stderr or retrieved.stdout
    payload = _json_payload(retrieved)
    rendered = json.dumps(payload) if payload else retrieved.stdout
    assert stored in rendered, (stored, retrieved.stdout, retrieved.stderr)


def _modality_request(
    client: httpx.Client,
    method: str,
    path: str,
    headers: dict[str, str],
    project_dir: Path,
) -> httpx.Response:
    if path == "/api/embeddings":
        return client.request(method, path, headers=headers, json={"input": ["hi"]})
    if path == "/api/llm/generate":
        return client.request(
            method,
            path,
            headers=headers,
            json={"prompt": "hi", "total_timeout_seconds": 1.0},
        )
    if path == "/api/llm/chat/completions":
        return client.request(
            method,
            path,
            headers=headers,
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "project_path": str(project_dir),
                "tool_policy": {
                    "cli": "gcode",
                    "tools": ["search"],
                    "allow_mutation": False,
                },
                "caller": "e2e-boundary",
                "request_id": str(uuid.uuid4()),
                "limits": {
                    "max_turns": 1,
                    "max_tool_calls": 1,
                    "max_bytes_per_tool_result": 256,
                    "tool_timeout_seconds": 1.0,
                    "loop_timeout_seconds": 2,
                },
            },
        )
    if path == "/api/llm/vision/extract":
        return client.request(
            method,
            path,
            headers=headers,
            files={"file": ("pixel.png", _TINY_PNG, "image/png")},
        )
    return client.request(
        method,
        path,
        headers=headers,
        files={"file": ("tone.webm", b"RIFF", "audio/webm")},
    )


def test_modality_identity_binding(boundary: BoundaryHarness) -> None:
    headers = boundary.grant_headers()
    with authenticated_daemon_client(boundary.daemon) as client:
        for method, path in _MODALITY_ROUTES:
            ok = _modality_request(client, method, path, headers, boundary.project_dir)
            ok_payload = ok.json() if ok.content else {}
            ok_code = ok_payload.get("code") or ok_payload.get("error")
            assert ok_code != "forged_identity", (path, ok.text)
            if ok.status_code == 401:
                assert ok_code == "wrong_capability", (path, ok.text)
            else:
                assert ok.status_code in {200, 400, 500, 503}, (
                    path,
                    ok.status_code,
                    ok.text,
                )
                assert ok.status_code != 422, (path, ok.text)
            forged = _modality_request(
                client,
                method,
                path,
                headers
                | {
                    "X-Gobby-Machine-Id": "forged-machine",
                    "X-Gobby-Project-Id": "forged-project",
                },
                boundary.project_dir,
            )
            assert forged.status_code == 401, (path, forged.text)
            assert (forged.json().get("code") or forged.json().get("error")) == "forged_identity"


def test_concurrent_renewal_race(boundary: BoundaryHarness, postgres_db: Any) -> None:
    older = boundary.grant
    older_generation = _postgres_generation(older)
    rotated = _rotate_interactive(postgres_db, boundary.home, older)
    newer = _handshake_grant(boundary.daemon, boundary.project_dir)
    newer_generation = _postgres_generation(newer)
    assert newer_generation == rotated
    assert newer_generation > older_generation
    expired = _rechecksum(older.model_copy(update={"expires_at": int(time.time()) - 5}))
    write_grant_file(boundary.grant_path, expired)
    proxy = _start_handshake_grant_proxy(boundary.daemon.http_url, [newer, older])
    try:
        persist_interactive_grant(boundary.home, proxy.url, expired)
        env = boundary.command_env({"GOBBY_DAEMON_URL": proxy.url})
        first = boundary.run("gcode", "--allow-stale", "search", "fixture", env=env)
        assert first.returncode == 0, first.stderr or first.stdout
        after_newer = _load_cached_grant(boundary.grant_path)
        assert _postgres_generation(after_newer) == newer_generation
        write_grant_file(boundary.grant_path, _age_past_half_ttl(after_newer))
        second = boundary.run("gcode", "--allow-stale", "search", "fixture", env=env)
        assert second.returncode == 0, second.stderr or second.stdout
    finally:
        proxy.close()

    cached = _load_cached_grant(boundary.grant_path)
    assert _postgres_generation(cached) == newer_generation
    persist_interactive_grant(boundary.home, boundary.daemon.http_url, cached)
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
    upstream = os.environ.get("GOBBY_TEST_QDRANT_URL", "http://127.0.0.1:6333")
    qdrant_proxy = _start_qdrant_key_proxy(upstream, "e2e")
    try:
        archived = _install_direct_datastores(boundary, qdrant_url=qdrant_proxy.url)
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
    finally:
        qdrant_proxy.close()


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

        admitted: dict[str, int | str | None] = {
            "status": None,
            "code": None,
            "error": None,
        }
        flag, admitted_path, release_path = _admit_barrier_paths(boundary.home)
        for leftover in (admitted_path, release_path, flag):
            leftover.unlink(missing_ok=True)
        flag.write_text("1")

        def _in_flight_invalidate() -> None:
            try:
                with httpx.Client(base_url=boundary.daemon.http_url, timeout=45.0) as client:
                    response = client.post(
                        "/api/code-index/invalidate",
                        headers=boundary.grant_headers(),
                        json={"project_id": E2E_PROJECT_ID},
                    )
                admitted["status"] = response.status_code
                payload = response.json() if response.content else {}
                admitted["code"] = payload.get("code") or payload.get("error")
            except Exception as exc:
                admitted["error"] = repr(exc)

        worker = threading.Thread(target=_in_flight_invalidate, daemon=True)
        worker.start()
        try:
            _wait_for_path(admitted_path)
            assert admitted["status"] is None
            headers = daemon_auth_headers(boundary.home)
            recover = httpx.post(
                f"{standby.http_url}/api/admin/lease/recover",
                headers=headers,
                params={"stale_after_seconds": 0},
                timeout=20.0,
            )
            assert recover.status_code == 200, recover.text
            release_path.write_text("1")
            worker.join(timeout=45)
            assert worker.is_alive() is False, admitted
            assert admitted["status"] == 409, admitted
            assert admitted["code"] in {"stale_epoch", "lease_not_held"}, admitted
            remaining = postgres_db.fetchone(
                """
                SELECT root_path FROM code_indexed_project_states
                 WHERE machine_id = %s AND project_id = %s
                """,
                (E2E_MACHINE_ID, E2E_PROJECT_ID),
            )
            assert remaining is not None
            assert remaining["root_path"] == str(boundary.project_dir)

            assert wait_for_daemon_health(standby.http_port, timeout=30.0), (
                f"promoted owner failed to serve\n--- log ---\n{standby.read_logs()}\n"
                f"--- error ---\n{standby.read_error_logs()}"
            )
            successor_grant = _handshake_grant(standby, boundary.project_dir, home=boundary.home)
            persist_interactive_grant(boundary.home, standby.http_url, successor_grant)
            successor_headers = {
                **headers,
                "X-Gobby-Runtime-Grant": encode_grant_header(successor_grant),
                "X-Gobby-Machine-Id": successor_grant.principal.machine_id,
                "X-Gobby-Caller-Project-Id": successor_grant.principal.project_id,
                "X-Gobby-Project-Id": successor_grant.principal.project_id,
                "X-Gobby-Session-Id": successor_grant.principal.session_id or "",
            }
            with httpx.Client(base_url=standby.http_url, timeout=20.0) as owner:
                committed = owner.post(
                    "/api/code-index/invalidate",
                    headers=successor_headers,
                    json={"project_id": E2E_PROJECT_ID},
                )
            assert committed.status_code in {200, 207}, committed.text
            gone = postgres_db.fetchone(
                """
                SELECT root_path FROM code_indexed_project_states
                 WHERE machine_id = %s AND project_id = %s
                """,
                (E2E_MACHINE_ID, E2E_PROJECT_ID),
            )
            assert gone is None
            with httpx.Client(base_url=boundary.daemon.http_url, timeout=5.0) as displaced:
                refused = displaced.post(
                    "/api/code-index/invalidate",
                    headers=boundary.grant_headers(),
                    json={"project_id": E2E_PROJECT_ID},
                )
            assert refused.status_code == 409, refused.text
        finally:
            release_path.write_text("1")
            flag.unlink(missing_ok=True)
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
