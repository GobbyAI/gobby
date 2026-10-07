"""Cancellation must finish before an owned provider may be terminated."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.events.wake import WakeDispatcher
from tests.e2e import composer_proof_cleanup as cleanup_module
from tests.e2e.composer_proof import ProofRefused, Surface
from tests.e2e.composer_proof_cleanup import CleanupControl, cancel_proof_retries, quiesce_proof
from tests.terminals.fakes import make_memory_terminal

pytestmark = pytest.mark.unit


async def pending_retry() -> None:
    await asyncio.Event().wait()


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("retire", [False, True])
async def test_cleanup_accepts_only_exact_owned_exited_readback(
    changed: bool, retire: bool, managed_composer_tmpdir: Path
) -> None:
    terminal = replace(
        make_memory_terminal(),
        state="exited",
        session_id=str(uuid4()),
        project_id="00000000-0000-0000-0000-000000000e2e",
        host_epoch=str(uuid4()),
    )
    surface = Surface(
        "claude",
        str(terminal.session_id),
        terminal.id,
        str(terminal.host_epoch),
        str(terminal.project_id),
    )
    lookup = AsyncMock(
        return_value=replace(terminal, host_epoch=str(uuid4())) if changed else terminal
    )
    dispatcher = WakeDispatcher(MagicMock(), MagicMock())
    retry = asyncio.create_task(pending_retry())
    dispatcher._composer_retries[surface.session_id] = retry
    with tempfile.TemporaryDirectory(prefix="c") as root:
        control = CleanupControl(Path(root) / "clean.sock", terminal_lookup=lookup)
        assert Path(root).parent == managed_composer_tmpdir
        assert len(bytes(control.socket)) < 104
        with patch.object(
            dispatcher,
            "_terminal_route_for_session",
            AsyncMock(return_value=SimpleNamespace(managed_terminal=None)),
        ):
            server = asyncio.create_task(control.serve(dispatcher))
            try:
                await asyncio.wait_for(control.started.wait(), 1)

                async def clean() -> None:
                    if retire:
                        await cleanup_module.retire_terminal(control.socket, surface)
                    else:
                        await quiesce_proof(control.socket, [surface])

                if changed:
                    with pytest.raises(ProofRefused, match="cleanup"):
                        await clean()
                    assert not control.quiesced
                    assert not retry.done()
                else:
                    await clean()
                    assert control.quiesced is not retire
                    assert retry.cancelled()
                lookup.assert_awaited_once_with(surface)
            finally:
                retry.cancel()
                await asyncio.gather(retry, return_exceptions=True)
                server.cancel()
                await asyncio.gather(server, return_exceptions=True)


async def test_cancellation_awaits_both_retry_owners() -> None:
    dispatcher = WakeDispatcher(MagicMock(), MagicMock())
    started = asyncio.Event()
    finished: list[str] = []

    async def retry(name: str) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(name)

    composer = asyncio.create_task(retry("composer"))
    refresh = asyncio.create_task(retry("refresh"))
    dispatcher._composer_retries["proof"] = composer
    dispatcher._deferred_refreshes["proof"] = refresh
    await started.wait()
    try:
        await cancel_proof_retries(dispatcher, frozenset({"proof"}))
        assert composer.cancelled() and refresh.cancelled()
        assert sorted(finished) == ["composer", "refresh"]
    finally:
        composer.cancel()
        refresh.cancel()
        await asyncio.gather(composer, refresh, return_exceptions=True)


async def test_foreign_retry_prevents_any_cancellation() -> None:
    dispatcher = WakeDispatcher(MagicMock(), MagicMock())
    task = asyncio.create_task(pending_retry())
    dispatcher._composer_retries["foreign"] = task
    try:
        with pytest.raises(ProofRefused, match="unowned"):
            await cancel_proof_retries(dispatcher, frozenset({"proof"}))
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_private_cleanup_refuses_an_unowned_retry(managed_composer_tmpdir: Path) -> None:
    dispatcher = WakeDispatcher(MagicMock(), MagicMock())
    retry = asyncio.create_task(pending_retry())
    dispatcher._composer_retries["foreign"] = retry
    with tempfile.TemporaryDirectory(prefix="c") as directory:
        control = CleanupControl(Path(directory) / "cleanup.sock")
        assert Path(directory).parent == managed_composer_tmpdir
        assert len(bytes(control.socket)) < 104
        control.schedule(dispatcher)
        try:
            async with asyncio.timeout(2):
                await control.started.wait()
                with pytest.raises(ProofRefused, match="unconfirmed"):
                    await quiesce_proof(control.socket, [])
            assert not retry.done()
        finally:
            retry.cancel()
            if control.task is not None:
                control.task.cancel()
                await asyncio.gather(control.task, return_exceptions=True)
            await asyncio.gather(retry, return_exceptions=True)
