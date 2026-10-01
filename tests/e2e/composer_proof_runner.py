"""Opt-in isolated runner bootstrap; never imported by production startup."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from gobby.agents.detection.matcher import CompiledManifest
from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.events.wake import WakeDispatcher
from gobby.storage.terminals import Terminal
from gobby.terminals.runtime import WriteOutcome
from gobby.terminals.write_coordinator import WriteCoordinator, WriteRequest
from tests.e2e.composer_proof import ProofRefused, require_private_root
from tests.e2e.composer_proof_cleanup import CleanupControl
from tests.e2e.composer_proof_trace import exchange_trace, observe_dispatch


def main() -> None:
    if os.environ.get("GOBBY_COMPOSER_PROOF_EXECUTION") != "PD_AUTHORIZED_ISOLATED_EXECUTION":
        raise ProofRefused("isolated execution grant required")
    socket = Path(os.environ["GOBBY_COMPOSER_PROOF_SOCKET"])
    require_private_root(socket.parent)
    cleanup = CleanupControl(socket.parent / "cleanup.sock")
    import gobby

    runtime: dict[str, Any] = {
        "runner_pid": os.getpid(),
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "gobby_module": gobby.__file__,
        "fixture_module": __file__,
        "detection": {},
    }
    manifest_for = DetectionManifestRegistry.for_provider
    runtime_lock = threading.Lock()

    def observed_manifest(
        registry: DetectionManifestRegistry, provider: str
    ) -> CompiledManifest | None:
        result = manifest_for(registry, provider)
        if result is not None:
            with runtime_lock:
                runtime["detection"][provider] = result.fingerprint
                target = socket.parent / "runtime.tmp"
                # Provider config or frame text never enters this runtime receipt.
                with target.open("w") as stream:
                    target.chmod(0o600)
                    json.dump(runtime, stream)
                target.replace(socket.parent / "runtime.json")
        return result

    bind_owner_loop = WakeDispatcher.bind_owner_loop

    def bound(dispatcher: WakeDispatcher, loop: asyncio.AbstractEventLoop) -> None:
        bind_owner_loop(dispatcher, loop)
        cleanup.schedule(dispatcher)

    dispatch = WriteCoordinator._dispatch

    async def traced(
        coordinator: WriteCoordinator, request: WriteRequest, terminal: Terminal | None = None
    ) -> WriteOutcome:
        if cleanup.quiesced and request.origin != "operator":
            raise ProofRefused("proof automatic writer quiesced")
        if terminal is None:
            terminal = await asyncio.to_thread(coordinator._require, request.terminal_id)

        async def original(write: WriteRequest, row: Terminal | None) -> WriteOutcome:
            return await dispatch(coordinator, write, row)

        async def exchange(event: dict[str, object]) -> None:
            await exchange_trace(socket, event)

        return await observe_dispatch(request, terminal, original, exchange)

    async def refuse_batch(*args: object, **kwargs: object) -> WriteOutcome:
        raise ProofRefused("this direct-session proof does not admit native fanout batches")

    wake = WakeDispatcher._dispatch_live_wake_unlocked

    async def traced_wake(
        dispatcher: WakeDispatcher,
        session_id: str,
        *,
        session: Any = None,
        priority: str = "normal",
        bypass_debounce: bool = False,
        prompt: str = "Message from Gobby daemon: New activity available.",
    ) -> dict[str, Any]:
        if cleanup.quiesced:
            return {"delivered": False, "skipped": "proof_quiesced"}
        started = time.monotonic()
        result = await wake(
            dispatcher,
            session_id,
            session=session,
            priority=priority,
            bypass_debounce=bypass_debounce,
            prompt=prompt,
        )
        observed = await asyncio.to_thread(dispatcher._session_manager.get, session_id)
        route = await dispatcher._terminal_route_for_session(observed)
        row = route.managed_terminal
        if not isinstance(row, Terminal):
            raise ProofRefused("proof wake has no exact managed terminal")
        await exchange_trace(
            socket,
            {
                "phase": "wake",
                "terminal_id": row.id,
                "session_id": row.session_id,
                "project_id": row.project_id,
                "host_epoch": row.host_epoch,
                "delivered": result.get("delivered") is True,
                "skipped": result.get("skipped"),
                "priority": priority,
                "monotonic": started,
            },
        )
        return result

    from gobby.runner import main as run

    with (
        patch.object(WriteCoordinator, "_dispatch", traced),
        patch.object(WriteCoordinator, "run_native_wake_batch", refuse_batch),
        patch.object(WakeDispatcher, "_dispatch_live_wake_unlocked", traced_wake),
        patch.object(WakeDispatcher, "bind_owner_loop", bound),
        patch.object(DetectionManifestRegistry, "for_provider", observed_manifest),
    ):
        run(config_path=Path(os.environ["GOBBY_CONFIG"]))


if __name__ == "__main__":
    main()
