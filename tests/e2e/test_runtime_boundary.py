"""Isolated-daemon boundary suite for plan 6.1."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from gobby.runtime_grants.handshake import encode_grant_header
from gobby.runtime_grants.launch import write_grant_file
from gobby.runtime_grants.schema import GrantBundle
from gobby.runtime_grants.signing import payload_checksum
from gobby.wiki.codewiki_dormant import CODEWIKI_DISABLED_REASON
from tests.e2e.conftest import (
    DaemonInstance,
    authenticated_daemon_client,
    daemon_auth_headers,
    daemon_health_unavailable,
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


def test_concurrent_renewal_race(boundary: BoundaryHarness) -> None:
    from concurrent.futures import ThreadPoolExecutor

    def renew() -> GrantBundle:
        return _handshake_grant(boundary.daemon, boundary.project_dir)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(lambda _: renew(), range(2))
    generations = {
        _postgres_generation(first),
        _postgres_generation(second),
    }
    assert max(generations) >= min(generations)
    write_grant_file(
        boundary.grant_path, first if first.expires_at >= second.expires_at else second
    )
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


def test_restore_replay_rejected(boundary: BoundaryHarness, postgres_db: Any) -> None:
    archived = boundary.grant
    snapshot = postgres_db.fetchone(
        """
        SELECT deployment_token, fencing_epoch, grant_signing_secret
        FROM deployment_runtime
        """
    )
    assert snapshot is not None
    token = snapshot["deployment_token"]
    epoch = snapshot["fencing_epoch"]
    secret = snapshot["grant_signing_secret"]
    if boundary.daemon.is_alive():
        boundary.daemon.stop()
    boundary.daemon.restart()
    assert wait_for_daemon_health(boundary.daemon.http_port)
    postgres_db.execute(
        """
        UPDATE deployment_runtime
        SET fencing_epoch = %s, grant_signing_secret = %s
        WHERE deployment_token = %s
        """,
        (epoch, secret, token),
    )
    if boundary.daemon.is_alive():
        boundary.daemon.stop()
    boundary.daemon.restart()
    assert wait_for_daemon_health(boundary.daemon.http_port)
    with authenticated_daemon_client(boundary.daemon) as client:
        presented = client.post(
            "/api/embeddings",
            headers={
                **daemon_auth_headers(boundary.home),
                "X-Gobby-Runtime-Grant": encode_grant_header(archived),
                "X-Gobby-Machine-Id": archived.principal.machine_id,
                "X-Gobby-Project-Id": archived.principal.project_id,
                "X-Gobby-Session-Id": archived.principal.session_id or "",
            },
            json={"texts": ["hi"]},
        )
    assert presented.status_code in {401, 403, 409}, presented.text
    write_grant_file(boundary.grant_path, archived)
    boundary.daemon.stop()
    offline = boundary.run("gcode", "--allow-stale", "status")
    assert offline.returncode == 0, offline.stderr or offline.stdout


def test_takeover_fencing(
    boundary: BoundaryHarness,
    e2e_project_dir: Path,
    postgres_database_url: str,
    postgres_schema: str,
    postgres_db: Any,
) -> None:
    import shutil

    from tests.e2e.conftest import _postgres_url_for_schema, find_free_port
    from tests.e2e.test_single_active_daemon import _spawn_daemon, _write_daemon_home

    # Same-home overlap cannot run: the pid file is per GOBBY_HOME. A second
    # home is a second deployment (independent lease). Same-deployment steal is
    # covered by tests/test_daemon_lease.py.
    standby_home = e2e_project_dir / ".gobby-standby"
    standby_home.mkdir()
    http_port = find_free_port()
    ws_port = find_free_port()
    config = _write_daemon_home(
        standby_home,
        database_url=_postgres_url_for_schema(postgres_database_url, postgres_schema),
        http_port=http_port,
        ws_port=ws_port,
        machine_id=STANDBY_MACHINE_ID,
    )
    for name in (".secret_kek", "local_cli_token"):
        src = boundary.home / name
        if src.is_file():
            shutil.copy2(src, standby_home / name)
    standby = _spawn_daemon(e2e_project_dir, config, http_port, ws_port)
    try:
        if not wait_for_daemon_health(http_port):
            pytest.fail(
                "standby daemon did not become healthy\n"
                f"alive={standby.is_alive()} exit={standby.process.poll()}\n"
                f"--- log ---\n{standby.read_logs()}\n"
                f"--- error ---\n{standby.read_error_logs()}"
            )
        with httpx.Client(
            base_url=f"http://127.0.0.1:{boundary.daemon.http_port}", timeout=5.0
        ) as old:
            refused = old.post(
                "/api/code-index/graph/rebuild",
                headers=boundary.grant_headers(),
                params={"project_id": E2E_PROJECT_ID},
            )
        assert refused.status_code in {401, 403, 404, 409, 503}, refused.text
        with authenticated_daemon_client(standby) as owner:
            served = owner.get("/api/auth/status")
        assert served.status_code == 200
    finally:
        standby.stop()


def test_rotation_drain_and_revocation(boundary: BoundaryHarness) -> None:
    first = boundary.grant
    rotated = _handshake_grant(boundary.daemon, boundary.project_dir)
    assert _postgres_generation(rotated) >= _postgres_generation(first)
    write_grant_file(boundary.grant_path, first)
    boundary.daemon.stop()
    drained = boundary.run("gcode", "--allow-stale", "status")
    assert drained.returncode == 0, drained.stderr or drained.stdout
    boundary.daemon.restart()
    assert wait_for_daemon_health(boundary.daemon.http_port)
    with authenticated_daemon_client(boundary.daemon) as client:
        revoked = client.post(
            "/api/embeddings",
            headers={
                **daemon_auth_headers(boundary.home),
                "X-Gobby-Runtime-Grant": encode_grant_header(first),
                "X-Gobby-Machine-Id": first.principal.machine_id,
                "X-Gobby-Project-Id": first.principal.project_id,
                "X-Gobby-Session-Id": first.principal.session_id or "",
            },
            json={"texts": ["hi"]},
        )
    code = revoked.json().get("code") or revoked.json().get("error")
    assert code in {"revoked", "stale_epoch", "invalid_signature", "expired"}
