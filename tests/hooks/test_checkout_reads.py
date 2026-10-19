"""Hub checkout-read seam: node origin, delivery memo, latch and git routing (#24522)."""

from __future__ import annotations

import asyncio
import base64
import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.hooks.adapter_execution import run_adapter_hook
from gobby.hooks.checkout_reads import (
    CheckoutReadUnavailable,
    delivery_scope,
    event_machine_id,
    node_origin,
    path_facts,
    run_git,
    run_git_async,
)
from gobby.hooks.events import HookResponse
from tests.hooks.checkout_read_fakes import HUB_MACHINE, NODE_MACHINE, FakeNode

pytestmark = pytest.mark.unit

_TIMEOUT = 5.0
_LATCHING = (
    "loop_thread",
    "node_routing_unavailable",
    "node_reply_lost",
    "node_offline",
    "node_timeout",
    "channel_closed",
)
_NON_LATCHING = ("no_project_root", "invalid_payload", "outside")


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> FakeNode:
    fake = FakeNode()
    fake.install(monkeypatch)
    return fake


def _adapter_payload(cwd: str, machine_id: str | None) -> dict[str, Any]:
    input_data: dict[str, Any] = {
        "source_event_id": "env-checkout-read",
        "tool_name": "Bash",
        "tool_input": {"command": "echo ok"},
        "cwd": cwd,
    }
    if machine_id is not None:
        input_data["machine_id"] = machine_id
    return {
        "_platform_session_id": "checkout-read-session",
        "hook_type": "pre-tool-use",
        "source": "claude",
        "input_data": input_data,
    }


def _observe() -> tuple[bool, str | None]:
    return node_origin(), event_machine_id()


async def _observe_async() -> tuple[bool, str | None]:
    return _observe()


@pytest.mark.asyncio
async def test_delivery_scope_marks_node_origin(node: FakeNode, tmp_path: Path) -> None:
    cwd = str(tmp_path)
    loop = asyncio.get_running_loop()
    assert node_origin() is False

    with delivery_scope(NODE_MACHINE, loop, cwd):
        assert node_origin() is True
        assert event_machine_id() == NODE_MACHINE
        assert await asyncio.to_thread(_observe) == (True, NODE_MACHINE)
        assert await asyncio.to_thread(asyncio.run, _observe_async()) == (True, NODE_MACHINE)

    for machine_id in (HUB_MACHINE, None):
        with delivery_scope(machine_id, loop, cwd):
            assert node_origin() is False
            assert event_machine_id() == HUB_MACHINE
    assert node_origin() is False

    seen: list[tuple[bool, str | None]] = []

    def handle(event: object) -> HookResponse:
        seen.append((node_origin(), event_machine_id()))
        return HookResponse(decision="allow")

    hook_manager = MagicMock()
    hook_manager.handle.side_effect = handle
    result = await run_adapter_hook(
        ClaudeCodeAdapter(),
        _adapter_payload(cwd, NODE_MACHINE),
        hook_manager,
        timeout_seconds=_TIMEOUT,
    )
    assert result == {"continue": True}
    assert seen == [(True, NODE_MACHINE)]
    assert node_origin() is False


@pytest.mark.asyncio
async def test_path_facts_queries_node_once_per_delivery(node: FakeNode, tmp_path: Path) -> None:
    cwd = str(tmp_path)
    alpha, beta, gamma = f"{cwd}/alpha.txt", f"{cwd}/beta.txt", f"{cwd}/gamma.txt"
    node.add_file(NODE_MACHINE, alpha, b"alpha")
    node.add_file(NODE_MACHINE, beta, b"beta")
    node.add_file(NODE_MACHINE, gamma, b"gamma")
    loop = asyncio.get_running_loop()

    with delivery_scope(NODE_MACHINE, loop, cwd):
        first = await asyncio.to_thread(path_facts, [alpha, beta], read_limit=64)
        assert [fact.path for fact in first.files] == [alpha, beta]
        assert [fact.data for fact in first.files] == [b"alpha", b"beta"]
        assert first.files[0].kind == "file"
        assert node.requests == [
            (
                NODE_MACHINE,
                "checkout.read",
                {"cwd": cwd, "query": {"kind": "files", "paths": [alpha, beta], "read_limit": 64}},
                5000,
            )
        ]

        repeat = await asyncio.to_thread(path_facts, [alpha], read_limit=64)
        assert [fact.data for fact in repeat.files] == [b"alpha"]
        assert len(node.requests) == 1

        await asyncio.to_thread(path_facts, [alpha, gamma], read_limit=64)
        assert len(node.requests) == 2
        assert node.requests[-1][2]["query"]["paths"] == [gamma]

    with delivery_scope(None, loop, cwd), pytest.raises(RuntimeError):
        path_facts([alpha], read_limit=64)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [*_LATCHING, *_NON_LATCHING])
async def test_unavailable_answers_latch_per_delivery(
    node: FakeNode, tmp_path: Path, code: str
) -> None:
    cwd = str(tmp_path)
    first_path, second_path = f"{cwd}/first.txt", f"{cwd}/second.txt"
    node.add_file(NODE_MACHINE, first_path, b"first")
    node.add_file(NODE_MACHINE, second_path, b"second")
    loop = asyncio.get_running_loop()
    on_loop = code == "loop_thread"
    if not on_loop:
        node.fail(NODE_MACHINE, code, "none" if code in _NON_LATCHING else None)

    with delivery_scope(NODE_MACHINE, loop, cwd):
        with pytest.raises(CheckoutReadUnavailable) as first:
            if on_loop:
                path_facts([first_path])
            else:
                await asyncio.to_thread(path_facts, [first_path])
        assert first.value.code == code
        assert first.value.machine_id == NODE_MACHINE
        assert str(first.value) == f"checkout_read_unavailable: {code} (machine {NODE_MACHINE})"
        sent = len(node.requests)
        assert sent == (0 if on_loop else 1)

        node.recover(NODE_MACHINE)
        if code in _LATCHING:
            with pytest.raises(CheckoutReadUnavailable) as later:
                await asyncio.to_thread(path_facts, [second_path])
            assert later.value.code == code
            assert len(node.requests) == sent
        else:
            facts = await asyncio.to_thread(path_facts, [second_path])
            assert [fact.data for fact in facts.files] == [b"second"]
            assert len(node.requests) == sent + 1


@pytest.mark.asyncio
async def test_run_git_routes_by_origin(node: FakeNode, tmp_path: Path) -> None:
    cwd = str(tmp_path)
    loop = asyncio.get_running_loop()
    subprocess.run(["git", "-C", cwd, "init", "-q"], check=True)

    with delivery_scope(None, loop, cwd):
        local = run_git(["rev-parse", "--is-inside-work-tree"], cwd=cwd, timeout=_TIMEOUT)
    assert local.returncode == 0
    assert local.stdout == b"true\n"
    assert node.requests == []

    node.set_git(NODE_MACHINE, stdout=b"abc\n")
    with delivery_scope(NODE_MACHINE, loop, cwd):
        done = await asyncio.to_thread(
            run_git, ["log", "-1"], cwd=cwd, timeout=7.5, input_bytes=b"in"
        )
        assert done.returncode == 0
        assert done.stdout == b"abc\n"
        assert done.stderr == b""
        assert node.requests[-1] == (
            NODE_MACHINE,
            "checkout.read",
            {
                "cwd": cwd,
                "query": {
                    "kind": "git",
                    "args": ["log", "-1"],
                    "stdin": base64.b64encode(b"in").decode("ascii"),
                    "timeout_ms": 7500,
                },
            },
            7500,
        )
        awaited = await run_git_async(["log", "-1"], cwd=cwd, timeout=7.5)
        assert awaited.stdout == b"abc\n"

        node.set_git(NODE_MACHINE, exit_code=128, stderr="fatal: not a repository\n")
        failed = await asyncio.to_thread(run_git, ["status"], cwd=cwd, timeout=_TIMEOUT)
        assert failed.returncode == 128
        assert failed.stderr == b"fatal: not a repository\n"

        node.set_git(NODE_MACHINE, exit_code=None, timed_out=True)
        with pytest.raises(subprocess.TimeoutExpired):
            await asyncio.to_thread(run_git, ["status"], cwd=cwd, timeout=_TIMEOUT)

        node.set_git(NODE_MACHINE, exit_code=0, stdout=b"x" * 16, truncated=True)
        with pytest.raises(CheckoutReadUnavailable) as cut:
            await asyncio.to_thread(run_git, ["ls-files"], cwd=cwd, timeout=_TIMEOUT)
        assert cut.value.code == "truncated"

        node.set_git(NODE_MACHINE, stdout=b"ok\n")
        after = await asyncio.to_thread(run_git, ["ls-files"], cwd=cwd, timeout=_TIMEOUT)
        assert after.stdout == b"ok\n"


@pytest.mark.asyncio
async def test_node_content_stays_in_delivery_memo(
    node: FakeNode, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cwd = str(tmp_path)
    secret_path = f"{cwd}/secret.txt"
    node.add_file(NODE_MACHINE, secret_path, b"SECRET-FILE-BODY")
    node.set_git(NODE_MACHINE, stdout=b"SECRET-GIT-OUTPUT\n")
    seen: list[tuple[bytes | None, bytes]] = []

    def read_inside_delivery(event: object) -> HookResponse:
        facts = path_facts([secret_path], read_limit=64)
        git = run_git(["log"], cwd=cwd, timeout=_TIMEOUT)
        seen.append((facts.files[0].data, git.stdout))
        return HookResponse(decision="allow")

    hook_manager = MagicMock()
    hook_manager.handle.side_effect = read_inside_delivery
    caplog.set_level(logging.DEBUG)
    result = await run_adapter_hook(
        ClaudeCodeAdapter(),
        _adapter_payload(cwd, NODE_MACHINE),
        hook_manager,
        timeout_seconds=_TIMEOUT,
    )

    # Positive control: the handler read the content, so the sink checks are not vacuous.
    assert seen == [(b"SECRET-FILE-BODY", b"SECRET-GIT-OUTPUT\n")]
    sinks = repr(hook_manager.mock_calls) + repr(result) + caplog.text
    for secret in (b"SECRET-FILE-BODY", b"SECRET-GIT-OUTPUT"):
        assert secret.decode("ascii") not in sinks
        assert base64.b64encode(secret).decode("ascii") not in sinks


@pytest.mark.asyncio
async def test_closed_loop_latches_channel_closed(node: FakeNode, tmp_path: Path) -> None:
    cwd = str(tmp_path)
    path = f"{cwd}/closed.txt"
    node.add_file(NODE_MACHINE, path, b"closed")
    closed = asyncio.new_event_loop()
    closed.close()

    with delivery_scope(NODE_MACHINE, closed, cwd):
        with pytest.raises(CheckoutReadUnavailable) as first:
            path_facts([path], read_limit=64)
        assert first.value.code == "channel_closed"
        assert first.value.machine_id == NODE_MACHINE
        with pytest.raises(CheckoutReadUnavailable) as later:
            path_facts([path], read_limit=64)
        assert later.value.code == "channel_closed"

    with delivery_scope(NODE_MACHINE, closed, cwd):
        with pytest.raises(CheckoutReadUnavailable) as awaited:
            await run_git_async(["status"], cwd=cwd, timeout=_TIMEOUT)
        assert awaited.value.code == "channel_closed"
    assert node.requests == []


def test_local_run_git_ignores_inherited_git_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    for checkout in (repo, other):
        checkout.mkdir()
        subprocess.run(["git", "-C", str(checkout), "init", "-q"], check=True)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))

    local = run_git(["rev-parse", "--absolute-git-dir"], cwd=str(repo), timeout=_TIMEOUT)

    assert local.returncode == 0
    assert Path(local.stdout.decode("utf-8").strip()).resolve() == (repo / ".git").resolve()
