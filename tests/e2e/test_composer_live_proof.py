"""Real Claude/Codex acceptance. Default collection performs no fixture setup.

Execution is a separately admitted operation; see docs/development/composer-live-proof.md.
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
    link_existing_auth,
    require_private_root,
    sealed_environment,
)
from tests.e2e.composer_proof_admission import (
    Admission,
    binary_readback,
    git_read,
    load_admission,
    verify_binary_set,
)
from tests.e2e.composer_proof_live import LiveProof
from tests.e2e.composer_proof_races import ComposerRaces
from tests.e2e.composer_proof_trace import ProofTrace
from tests.e2e.conftest import (
    DaemonInstance,
    prepare_daemon_env,
    terminate_process_tree,
    wait_for_daemon_health,
    wait_for_daemon_websocket,
)
from tests.e2e.test_terminal_client_stack import _open_control

_GRANT = "PD_AUTHORIZED_ISOLATED_EXECUTION"
_CHECKOUT = Path(__file__).resolve().parents[2]
_PROJECT = "00000000-0000-0000-0000-000000000e2e"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("GOBBY_COMPOSER_PROOF_EXECUTION") != _GRANT,
        reason="real-provider execution requires separate PD admission",
    ),
]


@pytest.fixture(autouse=True)
def composer_admission() -> Admission:
    if os.environ.get("GOBBY_COMPOSER_PROOF_EXECUTION") != _GRANT:
        raise ProofRefused("isolated execution grant required")
    config = os.environ.get("GOBBY_COMPOSER_PROOF_ADMISSION")
    if config is None:
        raise ProofRefused("reviewed proof admission file required")
    spec = load_admission(Path(config), _CHECKOUT)
    if spec.artifact_dir is None:
        raise ProofRefused("owner-private evidence directory required")
    require_private_root(spec.artifact_dir)
    common = Path(git_read(_CHECKOUT, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    if common.parent != _CHECKOUT and spec.worktree_test_notice_id is None:
        raise ProofRefused("announced worktree test exception receipt required")
    return spec


@pytest.fixture
async def composer_fixture(
    composer_admission: Admission,
    e2e_project_dir: Path,
    e2e_config: tuple[Path, int, int],
    postgres_db: Any,
) -> AsyncIterator[LiveProof]:
    spec = composer_admission
    native_bin = verify_binary_set(spec)
    config, http_port, ws_port = e2e_config
    home = config.parent
    home.chmod(0o700)
    require_private_root(home)
    (home / "tmp").mkdir(mode=0o700)
    providers = home / "providers"
    providers.mkdir(mode=0o700)
    for name in ("claude", "codex"):
        link_existing_auth(providers, name, spec.providers[name].auth_reference)
        (home / f".{name}").symlink_to(providers / name, target_is_directory=True)
    nested = home / ".gobby"
    nested.mkdir(mode=0o700, exist_ok=True)
    (nested / "bin").symlink_to(native_bin, target_is_directory=True)
    # Short private socket root accommodates macOS AF_UNIX limits.
    root = Path(tempfile.mkdtemp(prefix="p22915-", dir="/tmp"))
    host = root / "host"
    host.mkdir(mode=0o700)
    scope = ProofScope(
        root, _PROJECT, frozenset(map(str, spec.excluded_identities)), spec.reviewed_commit
    )
    trace = ProofTrace(scope, root / "trace.sock")
    mutations = ConfigMutations(postgres_db)
    mutations.patch_internal(
        expected_revision=mutations.repository.current_revision(),
        patch=ConfigPatch(
            values={
                "terminals.default_backend": "native",
                "terminal_host.socket_dir": str(host),
                "terminal_host.max_attachments_total": 8,
                "terminal_host.max_attachments_per_terminal": 4,
                "agent_sandbox.enabled": False,
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
        source="22915-isolated-composer-proof",
    )
    token = uuid4().hex
    for directory in (home, nested, host):
        target = directory / "local_cli_token"
        target.write_text(token)
        target.chmod(0o600)
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
        CODEX_HOME=str(home / ".codex"),
        CLAUDE_CONFIG_DIR=str(providers / "claude"),
        GOBBY_COMPOSER_PROOF_EXECUTION=_GRANT,
        GOBBY_COMPOSER_PROOF_SOCKET=str(trace.socket),
        GOBBY_ALLOW_WORKTREE_DAEMON="1",
    )
    # File-backed CLI authentication must not inherit even empty API-key overrides.
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        env.pop(name, None)
    readback: list[dict[str, object]] = []
    log = home / "logs" / "composer-daemon.log"
    errors = home / "logs" / "composer-daemon-error.log"
    command = [sys.executable, "-m", "tests.e2e.composer_proof_runner"]
    process: subprocess.Popen[bytes] | None = None
    proof: LiveProof | None = None
    clean = False
    try:
        readback = await asyncio.to_thread(binary_readback, spec, env)
        # Installers execute with the sealed temporary HOME; no real state is read/copied.
        await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "tests.e2e.composer_proof_setup", str(e2e_project_dir)],
            cwd=e2e_project_dir,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
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
        yield proof
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
            if clean and process is not None and process.poll() is None:
                await asyncio.to_thread(terminate_process_tree, process.pid)
            if spec.artifact_dir is not None:
                artifact = spec.artifact_dir / f"composer-proof-{uuid4()}.json"
                payload = {
                    "reviewed_commit": spec.reviewed_commit,
                    "reviewed_tree": spec.reviewed_tree,
                    "execution": "real-provider-opt-in",
                    "binary_readback": readback,
                    "criterion5": "PASSED"
                    if clean and proof is not None and proof.matrix_passed
                    else "FAILED_OR_REFUSED",
                    "cleanup_confirmed": clean,
                    "criterion6": "PASSED"
                    if clean and proof is not None and proof.races_passed
                    else "PENDING_OR_REFUSED: full real race matrix has not passed",
                    "surfaces": []
                    if proof is None
                    else [seat.surface.__dict__ for seat in proof.seats],
                    "created_terminal_ids": [] if proof is None else proof.created_terminal_ids,
                    "evidence": [] if proof is None else proof.evidence,
                    "writes_and_attempts": trace.events,
                    "isolated_state_retained": None
                    if clean
                    else {
                        "socket_root": str(root),
                        "home": str(home),
                        "project": str(e2e_project_dir),
                        "daemon_pid": None if process is None else process.pid,
                    },
                }
                with artifact.open("x") as stream:
                    artifact.chmod(0o600)
                    json.dump(payload, stream, indent=2)
            if clean:
                shutil.rmtree(root)
                shutil.rmtree(home)


async def test_real_claude_and_codex_composer_matrix(
    composer_fixture: LiveProof, composer_admission: Admission
) -> None:
    async with asyncio.timeout(90 * 60):
        for provider in ("claude", "codex"):
            await asyncio.to_thread(verify_binary_set, composer_admission)
            binary = composer_admission.providers[provider]
            prompt = (
                "Disposable read-only composer proof. Reply READY. Do not use tools or edit files."
            )
            command = (
                [str(binary.path), prompt, "--tools", "", "--disallowedTools", "mcp__*"]
                if provider == "claude"
                else [
                    str(binary.path),
                    "--sandbox",
                    "read-only",
                    "--ask-for-approval",
                    "never",
                    "-c",
                    "mcp_servers.gobby.enabled=false",
                    prompt,
                ]
            )
            seat = await composer_fixture.bind(provider, command)
            await composer_fixture.matrix(seat)
            submissions = [
                item
                for item in composer_fixture.evidence
                if item.get("session_id") == seat.surface.session_id
                and "submitted_prompt_sha256" in item
            ]
            assert len(submissions) == 4, "all empty/occupied normal/urgent submissions required"
            for fail in (False, True):
                for clear in (False, True):
                    for first in ("wake", "handoff"):
                        for cancel in (False, True):
                            target = (
                                await composer_fixture.bind(provider, command) if fail else seat
                            )
                            await composer_fixture.races.race(
                                target, clear=clear, first=first, cancel=cancel, fail=fail
                            )
        assert {seat.surface.provider for seat in composer_fixture.seats} == {"claude", "codex"}
        notices = [
            item["message_id"]
            for item in composer_fixture.evidence
            if item.get("case") in {"normal", "urgent"} and "message_id" in item
        ]
        assert len(notices) == 40 and len(set(notices)) == 40
        races = [item for item in composer_fixture.evidence if item.get("case") == "handoff_race"]
        assert len(races) == 32 and len({item["attempt_id"] for item in races}) == 32
        assert (
            len(
                {
                    (item["provider"], item["clear"], item["first"], item["cancel"], item["failed"])
                    for item in races
                }
            )
            == 32
        )
        await asyncio.to_thread(verify_binary_set, composer_admission)
        composer_fixture.matrix_passed = True
        composer_fixture.races_passed = True
