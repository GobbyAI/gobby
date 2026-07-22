from __future__ import annotations

from typing import Any

import pytest

from gobby.gwiki_gateway import GwikiGateway


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        timeout: bool = False,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timeout = timeout
        self.terminated = False
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.timeout:
            raise TimeoutError
        return self.stdout, self.stderr

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> None:
        self.waited = True


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    processes: list[FakeProcess],
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def create_subprocess(*args: str, **_kwargs: Any) -> FakeProcess:
        calls.append(args)
        return processes.pop(0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", create_subprocess)
    return calls


@pytest.mark.asyncio
async def test_reconciliation_commands_are_rootless_and_return_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_subprocess(
        monkeypatch,
        [
            FakeProcess(stdout=b"pruned"),
            FakeProcess(returncode=3, stderr=b"purge failed"),
        ],
    )
    gateway = GwikiGateway(
        binary="/bin/gwiki",
        project_root="/repo",
        topic="docs",
    )

    prune = await gateway.prune_all_scopes(timeout=1.5)
    purge = await gateway.purge_project_scope(
        "11111111-1111-4111-8111-111111111111",
        timeout=2.5,
    )

    assert calls == [
        ("/bin/gwiki", "prune", "--force"),
        (
            "/bin/gwiki",
            "purge",
            "--project-id",
            "11111111-1111-4111-8111-111111111111",
            "--yes",
        ),
    ]
    assert prune.success is True
    assert prune.returncode == 0
    assert prune.stdout == "pruned"
    assert prune.timeout_seconds == 1.5
    assert purge.success is False
    assert purge.returncode == 3
    assert purge.stderr == "purge failed"
    assert purge.timeout_seconds == 2.5


@pytest.mark.asyncio
async def test_reconciliation_timeout_returns_failed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(timeout=True)
    _patch_subprocess(monkeypatch, [process])

    result = await GwikiGateway(binary="/bin/gwiki").prune_all_scopes(timeout=0.01)

    assert result.success is False
    assert result.returncode is None
    assert result.timed_out is True
    assert result.timeout_seconds == 0.01
    assert "gwiki timed out after 0.01s" in result.stderr
    assert process.terminated is True
    assert process.waited is True
