"""Handoff-only fixture; preserves the separately admitted composer proof tree.

Only Gobby state is temporary. Seats use normal provider homes and installed hooks.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.storage.config_mutations import ConfigMutations, ConfigPatch
from gobby.workflows.state_manager import SessionVariableManager
from tests.e2e.composer_proof import (
    ProofRefused,
    ProofScope,
    require_private_root,
    sealed_environment,
)
from tests.e2e.composer_proof_live import LiveProof
from tests.e2e.composer_proof_races import ComposerRaces
from tests.e2e.composer_proof_trace import ProofTrace
from tests.e2e.conftest import (
    DaemonInstance,
    copy_daemon_api_key,
    native_daemon_command,
    prepare_daemon_env,
    terminate_process_tree,
    wait_for_daemon_health,
    wait_for_daemon_websocket,
)
from tests.e2e.handoff_continuation_admission import (
    Admission,
    binary_readback,
    load_admission,
    verify_binary_set,
)
from tests.e2e.handoff_continuation_barrier import HandoffBarrier
from tests.e2e.handoff_continuation_launch import seat_environment
from tests.e2e.handoff_continuation_trust import require_provider_trust
from tests.e2e.test_terminal_client_stack import _open_control

_GRANT = "PD_AUTHORIZED_ISOLATED_EXECUTION"
_CHECKOUT = Path(__file__).resolve().parents[2]
_PROJECT = "00000000-0000-0000-0000-000000000e2e"


@pytest.fixture(scope="module", autouse=True)
def handoff_admission() -> Admission:
    if os.environ.get("GOBBY_HANDOFF_PROOF_EXECUTION") != _GRANT:
        raise ProofRefused("isolated handoff execution grant required")
    config = os.environ.get("GOBBY_HANDOFF_PROOF_ADMISSION")
    if config is None:
        raise ProofRefused("reviewed handoff admission file required")
    spec = load_admission(Path(config), _CHECKOUT)
    require_private_root(spec.artifact_dir)
    return spec


@pytest.fixture
def provider_environment(
    e2e_config: tuple[Path, int, int], handoff_admission: Admission
) -> dict[str, str]:
    config, port, _ = e2e_config
    native_bin = verify_binary_set(handoff_admission)
    env = seat_environment(dict(os.environ), home=config.parent, port=port)
    if not env.get("HOME"):
        raise ProofRefused("normal operator HOME required for provider login")
    env.update(
        GOBBY_PROJECT_ID=_PROJECT,
        GOBBY_NATIVE_BIN_DIR=str(native_bin),
        GOBBY_CONFIG=str(config),
        # The admission pins provider bytes; a seat must not self-update mid-proof.
        DISABLE_AUTOUPDATER="1",
    )
    env["PATH"] = f"{native_bin}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}"
    return env


@pytest.fixture
async def handoff_fixture(
    handoff_admission: Admission,
    provider_environment: dict[str, str],
    provider: str,
    scenario: str,
    e2e_project_dir: Path,
    e2e_config: tuple[Path, int, int],
    postgres_db: Any,
) -> AsyncIterator[tuple[LiveProof, HandoffBarrier]]:
    spec = handoff_admission
    native_bin = verify_binary_set(spec)
    config, http_port, ws_port = e2e_config
    home = config.parent
    home.chmod(0o700)
    require_private_root(home)
    (home / "tmp").mkdir(mode=0o700)
    nested = home / ".gobby"
    nested.mkdir(mode=0o700, exist_ok=True)
    (nested / "bin").symlink_to(native_bin, target_is_directory=True)
    # Short private socket root accommodates macOS AF_UNIX limits.
    root = Path(tempfile.mkdtemp(prefix="p"))
    host = root / "h"
    host.mkdir(mode=0o700)
    scope = ProofScope(
        root, _PROJECT, frozenset(map(str, spec.excluded_identities)), spec.reviewed_commit
    )
    trace = ProofTrace(scope, root / "trace.sock")
    barrier = HandoffBarrier(root / "hold.sock")
    mutations = ConfigMutations(postgres_db)
    mutations.patch_internal(
        expected_revision=mutations.repository.current_revision(),
        patch=ConfigPatch(
            values={
                "terminal_host.socket_dir": str(host),
                "terminal_host.max_attachments_total": 8,
                "terminal_host.max_attachments_per_terminal": 4,
                "tmux.auto_enter_approval_prompts": False,
                "tmux.auto_enter_agent_terminals": False,
                "hook_extensions.websocket.enabled": True,
                "hook_extensions.websocket.include_payload": True,
                "hook_extensions.websocket.broadcast_events": [
                    "user-prompt-submit",
                    "session-start",
                    "post-compact",
                    "stop",
                ],
            }
        ),
        source="25771-isolated-handoff-proof",
    )
    for directory in (home, nested, host):
        copy_daemon_api_key(home, directory)
    env = sealed_environment(dict(os.environ), home=home, native_bin=native_bin)
    bins = [binary.path.parent for binary in (*spec.providers.values(), *spec.support_tools)]
    env["PATH"] = (
        os.pathsep.join(map(str, dict.fromkeys([native_bin, *bins])))
        + ":/usr/bin:/bin:/usr/sbin:/sbin"
    )
    env["PYTHONPATH"] = str(_CHECKOUT)
    env = prepare_daemon_env(base_env=env, home_dir=home)
    env.update(
        GOBBY_CONFIG=str(config),
        GOBBY_HOME=str(home),
        GOBBY_DAEMON_URL=f"http://127.0.0.1:{http_port}",
        GOBBY_COMPOSER_PROOF_EXECUTION=_GRANT,
        GOBBY_COMPOSER_PROOF_SOCKET=str(trace.socket),
        GOBBY_ALLOW_WORKTREE_DAEMON="1",
    )
    # File-backed CLI authentication must not inherit even empty API-key overrides.
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        env.pop(name, None)
    if scenario == "restart_hold":
        env["GOBBY_HANDOFF_BARRIER_SOCKET"] = str(barrier.socket)
    readback: list[dict[str, object]] = []
    log = home / "logs" / "daemon.log"
    errors = home / "logs" / "daemon_error.log"
    command = native_daemon_command(
        env, [sys.executable, "-m", "tests.e2e.handoff_continuation_runner"]
    )
    process: subprocess.Popen[bytes] | None = None
    proof: LiveProof | None = None
    clean = False
    try:
        readback = await asyncio.to_thread(binary_readback, spec, env, provider_environment)
        # The real CLI needs workspace trust or it waits at its trust prompt. The proof never
        # writes the operator's provider config; it reads the fixed folder's trust and refuses.
        require_provider_trust(provider, e2e_project_dir, home=Path.home())
        (e2e_project_dir / "AGENTS.md").write_text(
            "Disposable handoff acceptance seat. Follow the launch prompt. Never edit files, "
            "run shell commands, spawn agents or contact other sessions.\n"
        )
        await trace.start()
        with log.open("wb") as output, errors.open("wb") as error:
            process = subprocess.Popen(
                command,
                cwd=e2e_project_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=error,
                start_new_session=True,
            )
        daemon = DaemonInstance(
            process,
            process.pid,
            http_port,
            ws_port,
            e2e_project_dir,
            e2e_project_dir / ".gobby",
            log,
            errors,
            home / "hub-postgres.db",
            config,
            command,
            env,
        )
        await asyncio.to_thread(wait_for_daemon_health, http_port, log_file=log, process=process)
        if not await asyncio.to_thread(wait_for_daemon_websocket, ws_port, home, timeout=10):
            raise ProofRefused("isolated WebSocket did not become healthy")
        proof = LiveProof(daemon, scope, trace, DetectionManifestRegistry(postgres_db))
        proof.races = ComposerRaces(proof, SessionVariableManager(postgres_db))
        trace.authorize_clear = proof.races.authorize_clear
        await proof.initialize()
        async with barrier:
            yield proof, barrier
    finally:
        try:
            if proof is not None:
                await proof.close()
                if proof.created_terminal_ids:
                    control = await _open_control(host)
                    try:
                        epochs = {seat.surface.host_epoch for seat in proof.seats}
                        if epochs != {control.host_epoch}:
                            raise ProofRefused("host cleanup epoch changed")
                        inventory = await control.list_inventory()
                        if inventory.epoch != control.host_epoch or inventory.rows:
                            raise ProofRefused("host still owns terminal rows; preserve it")
                        await control.host_shutdown(grace_ms=1000)
                    finally:
                        await control.close()
            elif process is not None:
                # A startup failure has no registered proof manifest. Preserve its host.
                raise ProofRefused("partial daemon startup requires owner cleanup")
            clean = True
        finally:
            await trace.close()
            if not clean and proof is not None:
                # A failed case still stops its private host (and the real seat in it) so it
                # cannot hold the shared test hub and fail the next case; logs are kept below.
                try:
                    control = await _open_control(host)
                    try:
                        await control.host_shutdown(grace_ms=1000)
                    finally:
                        await control.close()
                except (OSError, RuntimeError, ValueError, TimeoutError):
                    pass
            if process is not None and process.poll() is None:
                await asyncio.to_thread(terminate_process_tree, process.pid)
            if spec.artifact_dir is not None:
                artifact = spec.artifact_dir / f"handoff-proof-{uuid4()}.json"
                retained_logs = artifact.with_suffix(".logs")
                retained_logs.mkdir(mode=0o700)
                # Preserve diagnostics before the parent fixture deletes HOME. Never
                # copy state/configuration or follow a link out of the log directory.
                for source in log.parent.glob("*.log"):
                    if source.is_file() and not source.is_symlink():
                        target = retained_logs / source.name
                        with target.open("xb") as stream:
                            target.chmod(0o600)
                            with source.open("rb") as original:
                                shutil.copyfileobj(original, stream)
                payload = {
                    "reviewed_commit": spec.reviewed_commit,
                    "reviewed_tree": spec.reviewed_tree,
                    "execution": "real-provider-opt-in",
                    "isolated_daemon_url": provider_environment["GOBBY_DAEMON_URL"],
                    "normal_provider_homes": True,
                    "provider": provider,
                    "scenario": scenario,
                    "binary_readback": readback,
                    "continuation": "PASSED"
                    if clean and proof is not None and proof.matrix_passed
                    else "FAILED_OR_REFUSED",
                    "cleanup_confirmed": clean,
                    "surfaces": []
                    if proof is None
                    else [seat.surface.__dict__ for seat in proof.seats],
                    "created_terminal_ids": [] if proof is None else proof.created_terminal_ids,
                    "evidence": [] if proof is None else proof.evidence,
                    "writes_and_attempts": trace.events,
                    "trace_refusals": trace.refusals,
                    "retained_logs": str(retained_logs),
                    "isolated_state_retained": None
                    if clean
                    else {
                        "socket_root": str(root),
                        "daemon_pid": None if process is None else process.pid,
                    },
                }
                with artifact.open("x") as stream:
                    artifact.chmod(0o600)
                    json.dump(payload, stream, indent=2)
            if clean:
                shutil.rmtree(root)
                # The daemon installs its sandbox runtime as a read-only tree under HOME; the
                # parent TemporaryDirectory restores write bits and removes what is left.
                shutil.rmtree(home, ignore_errors=True)
