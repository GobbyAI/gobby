"""Proof safety checks run without launching providers, hosts or daemons."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from tests.e2e.composer_proof import (
    ProofRefused,
    ProofScope,
    Surface,
    cleanup_surfaces,
    link_existing_auth,
    owned_editor_cleanup,
    require_execution,
    safe_evidence,
)

pytestmark = pytest.mark.unit


def surface(provider: str = "claude") -> Surface:
    return Surface(provider, str(uuid4()), str(uuid4()), "private-epoch", str(uuid4()))


def test_only_exact_disjoint_surface_can_be_used(tmp_path: Path) -> None:
    own = surface()
    scope = ProofScope(tmp_path, own.project_id, frozenset({str(uuid4())}), "a" * 40)
    scope.require_surface(own, own)
    foreign = Surface(own.provider, own.session_id, str(uuid4()), own.host_epoch, own.project_id)
    with pytest.raises(ProofRefused, match="binding"):
        scope.require_surface(own, foreign)
    protected = ProofScope(tmp_path, own.project_id, frozenset({own.terminal_id}), "a" * 40)
    with pytest.raises(ProofRefused, match="excluded"):
        protected.require_surface(own, own)
    wrong_project = ProofScope(tmp_path, str(uuid4()), frozenset({str(uuid4())}), "a" * 40)
    with pytest.raises(ProofRefused, match="project"):
        wrong_project.require_surface(own, own)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_cleanup_only_erases_the_exact_owned_synthetic_draft(provider: str) -> None:
    text = f"R2_22915_{provider.upper()}_DRAFT_ABCD_EFGH"
    keys = owned_editor_cleanup(provider, text, 5)
    assert keys == "\x1b[C" * 5 + "\x7f" * len(text)
    assert "\r" not in keys and "\n" not in keys and "\x03" not in keys
    with pytest.raises(ProofRefused, match="draft"):
        owned_editor_cleanup(provider, text + "human", 5)
    with pytest.raises(ProofRefused, match="cursor"):
        owned_editor_cleanup(provider, text, 0)


def test_auth_is_a_reference_and_cleanup_does_not_touch_the_original(tmp_path: Path) -> None:
    original = tmp_path / "auth.json"
    original.write_text('{"fake-test-token": "not-a-real-secret"}')
    original.chmod(0o600)
    root = tmp_path / "proof"
    root.mkdir(mode=0o700)
    linked = link_existing_auth(root, "codex", original)
    assert linked.is_symlink()
    assert linked.resolve() == original
    linked.unlink()
    assert original.read_text() == '{"fake-test-token": "not-a-real-secret"}'


def test_auth_refuses_public_permissions_and_existing_config(tmp_path: Path) -> None:
    original = tmp_path / "auth.json"
    original.write_text("fake-test-token")
    original.chmod(0o644)
    root = tmp_path / "proof"
    root.mkdir(mode=0o700)
    with pytest.raises(ProofRefused, match="private"):
        link_existing_auth(root, "codex", original)
    original.chmod(0o600)
    link_existing_auth(root, "codex", original)
    with pytest.raises(ProofRefused, match="exists"):
        link_existing_auth(root, "codex", original)


def test_artifacts_never_serialize_raw_frames_or_auth_data() -> None:
    own = surface()
    frame = "\x1b[2Jprovider-secret-not-for-artifacts\n❯ R2_22915_CLAUDE_DRAFT_ABCD_EFGH"
    evidence = safe_evidence(own, frame, (3, 9))
    assert evidence["frame_sha256"] == hashlib.sha256(frame.encode()).hexdigest()
    assert evidence["cursor"] == [3, 9]
    assert "provider-secret" not in str(evidence)
    assert evidence["terminal_id"] == own.terminal_id


def test_execution_requires_exact_reviewed_commit_and_opt_in(tmp_path: Path) -> None:
    own = surface()
    scope = ProofScope(tmp_path, own.project_id, frozenset({str(uuid4())}), "a" * 40)
    with pytest.raises(ProofRefused, match="authorization"):
        require_execution(scope, "a" * 40, "")
    with pytest.raises(ProofRefused, match="commit"):
        require_execution(scope, "b" * 40, "PD_AUTHORIZED_ISOLATED_EXECUTION")
    require_execution(scope, "a" * 40, "PD_AUTHORIZED_ISOLATED_EXECUTION")


@pytest.mark.asyncio
async def test_cleanup_cancels_retries_and_refuses_rebound_terminal(tmp_path: Path) -> None:
    own = surface()
    scope = ProofScope(tmp_path, own.project_id, frozenset({str(uuid4())}), "a" * 40)
    cancelled = asyncio.Event()
    started = asyncio.Event()
    stopped: list[str] = []

    async def retry() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def rebound(expected: Surface) -> Surface:
        return Surface(
            expected.provider,
            str(uuid4()),
            expected.terminal_id,
            expected.host_epoch,
            expected.project_id,
        )

    async def stop(expected: Surface) -> None:
        stopped.append(expected.terminal_id)

    task = asyncio.create_task(retry())
    await asyncio.wait_for(started.wait(), 1)
    try:
        with pytest.raises(ProofRefused, match="binding"):
            await cleanup_surfaces(scope, [own], rebound, stop, [task])
        assert cancelled.is_set() and task.done()
        assert stopped == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
