"""Contained Python runners consume native authority without owning its lease."""

import ast
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import psycopg
import pytest

from gobby.config.bootstrap import BootstrapConfig
from gobby.runner import GobbyRunner, run_gobby
from gobby.runner_pid_file import ProbeState, claim_pid_file, probe_daemon_lock
from gobby.runtime_grants.lease_view import RuntimeLeaseView, current_lease
from gobby.servers.auth_service import AuthService
from gobby.servers.grant_auth import LiveLeaseGrantService
from gobby.servers.http import HTTPServer

pytestmark = pytest.mark.integration


@pytest.mark.unit
@pytest.mark.parametrize("supplied_claim", [False, True])
async def test_node_bootstrap_refuses_before_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supplied_claim: bool
) -> None:
    pid_file = tmp_path / "gobby.pid"
    ownership = claim_pid_file(pid_file) if supplied_claim else None
    if supplied_claim:
        assert ownership is not None
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    bootstrap = BootstrapConfig(
        datastore_mode="remote", hub_daemon_url="http://hub.example.test:60887"
    )
    monkeypatch.setattr("gobby.config.bootstrap.load_bootstrap", lambda *_a, **_kw: bootstrap)
    reached: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> None:
        reached.append("runtime authority")
        raise AssertionError("a node must refuse before native lifecycle or schema work")

    monkeypatch.setattr("gobby.storage.schema_contract.verify_schema", forbidden)
    monkeypatch.setattr("gobby.runtime_grants.lease_view.RuntimeLeaseView", forbidden)
    monkeypatch.setattr("gobby.runner_backend.watch_supervisor", forbidden)
    create = AsyncMock(side_effect=AssertionError("a node must not construct a runner"))
    monkeypatch.setattr(GobbyRunner, "create", create)
    with pytest.raises(RuntimeError) as refused:
        await run_gobby(ownership_resolution=ownership)
    assert str(refused.value) == (
        "datastore_mode: remote is a node; a node runs no Python daemon and never takes "
        "the hub lease. See docs/guides/shared-stack.md (Client setup)."
    )
    assert reached == []
    create.assert_not_awaited()
    assert not pid_file.exists()
    assert probe_daemon_lock(pid_file).state is ProbeState.ABSENT


@pytest.mark.unit
def test_runner_has_no_lease_paths() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src/gobby"
    retired = (
        "daemon_lease.py",
        "daemon_lease_control.py",
        "servers/lease_fence.py",
        "cli/daemon_lease.py",
        "servers/admit_barrier.py",
    )
    for relative in retired:
        assert not (source_root / relative).exists()
    retired_modules = {"gobby." + path.removesuffix(".py").replace("/", ".") for path in retired}
    for source_file in source_root.rglob("*.py"):
        for node in ast.walk(ast.parse(source_file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert not retired_modules.intersection(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module not in retired_modules
                assert not retired_modules.intersection(
                    f"{node.module}.{alias.name}" for alias in node.names
                )
    runner_source = (source_root / "runner.py").read_text(encoding="utf-8")
    for retired_path in (
        "ActiveDaemonLease",
        "acquire_windows_lease",
        "run_windows_backend",
        "effect_fence",
    ):
        assert retired_path not in runner_source


@pytest.mark.unit
@pytest.mark.parametrize("supplied_claim", [False, True])
async def test_native_windows_local_start_refuses_before_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supplied_claim: bool
) -> None:
    from gobby.runner_init import StartRefusal

    pid_file = tmp_path / "gobby.pid"
    ownership = claim_pid_file(pid_file) if supplied_claim else None
    if supplied_claim:
        assert ownership is not None
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "gobby.config.bootstrap.load_bootstrap",
        lambda *_args, **_kwargs: BootstrapConfig(database_url="postgresql://unused/test"),
    )
    create = AsyncMock()
    monkeypatch.setattr(GobbyRunner, "create", create)
    with pytest.raises(StartRefusal, match="WSL 2"):
        await run_gobby(ownership_resolution=ownership)
    create.assert_not_awaited()
    assert not pid_file.exists()
    assert probe_daemon_lock(pid_file).state is ProbeState.ABSENT


@pytest.fixture
def native_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    assert os.environ.get("GOBBY_TEST_PROTECT") == "1"
    database_url = os.environ["DATABASE_URL"]
    assert "60892/gobby_test" in database_url
    token = str(uuid4())
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.delenv("GOBBY_SUPERVISOR_FD", raising=False)
    monkeypatch.setattr("gobby.utils.local_token._daemon_bootstrap", None)
    monkeypatch.setattr(
        "gobby.config.bootstrap.load_bootstrap",
        lambda *_args, **_kwargs: BootstrapConfig(database_url=database_url),
    )
    monkeypatch.setattr("gobby.deployment.deployment_token", lambda: token)
    monkeypatch.setattr("gobby.utils.machine_id.require_machine_id", lambda: "contained-machine")
    monkeypatch.setattr("gobby.storage.schema_contract.verify_schema", lambda _dsn: None)
    monkeypatch.setattr("gobby.utils.break_glass.ensure_break_glass_credential", lambda _home: None)
    monkeypatch.setattr("gobby.providers.version_gate.probe_and_publish_agy_support", AsyncMock())
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS deployment_runtime (deployment_token TEXT PRIMARY KEY, fencing_epoch BIGINT NOT NULL DEFAULT 0, grant_signing_secret TEXT NOT NULL, epoch_updated_at TIMESTAMPTZ)"
        )
        connection.execute(
            "INSERT INTO deployment_runtime (deployment_token, fencing_epoch, grant_signing_secret) VALUES (%s, %s, %s)",
            (token, 7, "native-seeded-secret"),
        )
    try:
        yield database_url, token
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                "DELETE FROM deployment_runtime WHERE deployment_token = %s", (token,)
            )


def test_view_reads_seeded_row_without_acquiring(native_authority: tuple[str, str]) -> None:
    database_url, token = native_authority
    view = RuntimeLeaseView(database_url, machine_id="contained-machine", deployment_token=token)
    assert not hasattr(view, "try_acquire")
    try:
        view.bind()
        assert current_lease() is view
        assert view.fencing_epoch == 7
        assert view.grant_signing_secret == "native-seeded-secret"
        with psycopg.connect(database_url, autocommit=True) as connection:
            row = connection.execute(
                "SELECT fencing_epoch, grant_signing_secret FROM deployment_runtime WHERE deployment_token = %s",
                (token,),
            ).fetchone()
        assert row == (7, "native-seeded-secret")
    finally:
        view.release()
    assert current_lease() is None
    assert view.fencing_epoch is None
    assert view.grant_signing_secret is None


@pytest.mark.parametrize("fail_startup", [False, True])
@pytest.mark.parametrize("supervised", [False, True])
@pytest.mark.parametrize("hub", [False, True], ids=["standalone", "hub"])
async def test_contained_runner_seeds_row_and_clears_view(
    native_authority: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_startup: bool,
    supervised: bool,
    hub: bool,
) -> None:
    database_url, token = native_authority
    monkeypatch.setattr(
        "gobby.config.bootstrap.load_bootstrap",
        lambda *_args, **_kwargs: BootstrapConfig(database_url=database_url, hub=hub),
    )
    runner = MagicMock(spec=GobbyRunner)
    server = MagicMock(spec=HTTPServer)
    server.auth_service = MagicMock(spec=AuthService)
    runner.http_server = server
    runner.config_runtime = MagicMock()
    runner.database = None
    runner.managed_credential_manager = None
    runner.secret_store = None
    views: list[RuntimeLeaseView] = []

    async def run(*, ownership_resolution: object) -> None:
        view = current_lease()
        assert view is not None
        views.append(view)
        assert runner.daemon_lease is view
        assert view.fencing_epoch == 7
        assert isinstance(server.grant_service, LiveLeaseGrantService)
        expected_pid_state = ProbeState.ABSENT if supervised else ProbeState.DAEMON
        assert probe_daemon_lock(tmp_path / "gobby.pid").state is expected_pid_state
        server.auth_service.bind_runtime.assert_called_once()
        if fail_startup:
            raise RuntimeError("injected startup failure")

    runner.run = AsyncMock(side_effect=run)
    monkeypatch.setattr(GobbyRunner, "create", AsyncMock(return_value=runner))
    write_fd: int | None = None
    if supervised:
        read_fd, write_fd = os.pipe()
        monkeypatch.setenv("GOBBY_SUPERVISOR_FD", str(read_fd))
    try:
        if fail_startup:
            with pytest.raises(RuntimeError, match="injected startup failure"):
                await run_gobby()
        else:
            await run_gobby()
    finally:
        if write_fd is not None:
            os.close(write_fd)
    assert current_lease() is None
    assert len(views) == 1
    assert not views[0].owns_live_lease()
    assert views[0].fencing_epoch is None
    assert views[0].grant_signing_secret is None
    assert not (tmp_path / "gobby.pid").exists()
    assert probe_daemon_lock(tmp_path / "gobby.pid").state is ProbeState.ABSENT
    with psycopg.connect(database_url, autocommit=True) as connection:
        row = connection.execute(
            "SELECT fencing_epoch, grant_signing_secret FROM deployment_runtime WHERE deployment_token = %s",
            (token,),
        ).fetchone()
    assert row == (7, "native-seeded-secret")


async def test_create_refusal_clears_bound_view_and_contained_pid(
    native_authority: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.runner_init import StartRefusal

    monkeypatch.setattr(
        GobbyRunner, "create", AsyncMock(side_effect=StartRefusal("injected refusal"))
    )
    with pytest.raises(StartRefusal, match="injected refusal"):
        await run_gobby()
    assert current_lease() is None
    assert not (tmp_path / "gobby.pid").exists()
    assert probe_daemon_lock(tmp_path / "gobby.pid").state is ProbeState.ABSENT
    database_url, token = native_authority
    with psycopg.connect(database_url, autocommit=True) as connection:
        row = connection.execute(
            "SELECT fencing_epoch, grant_signing_secret FROM deployment_runtime WHERE deployment_token = %s",
            (token,),
        ).fetchone()
    assert row == (7, "native-seeded-secret")
