"""Scripted node answers for checkout-read tests (#24522).

FakeNode stands in for the hub-mode 1.3 node command client. It answers
`checkout.read` files and git queries per machine, can fail any machine with a
named error, and records every request it receives.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Literal

import pytest

from gobby.hooks import checkout_reads
from gobby.hub.node_client import NodeCommandError

HUB_MACHINE = "machine-hub"
NODE_MACHINE = "machine-b"
_PROJECT_ID = "project-b"
_HOME = "/home/node"
_TEMP_DIR = "/tmp"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@dataclass
class FakeNode:
    """Scripted per-machine answers plus a log of every request sent."""

    files: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    git: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: dict[str, tuple[str, Literal["none", "partial"] | None]] = field(default_factory=dict)
    requests: list[tuple[str, str, dict[str, Any], int]] = field(default_factory=list)

    def add_file(self, machine_id: str, path: str, data: bytes) -> None:
        entry = {
            "path": path,
            "resolved": path,
            "kind": "file",
            "checkout_root": "/checkout",
            "git_common_dir": "/checkout/.git",
            "project_root": "/checkout",
            "project_id": _PROJECT_ID,
            "data": _b64(data),
            "truncated": False,
        }
        self.files.setdefault(machine_id, {})[path] = entry

    def set_git(
        self,
        machine_id: str,
        *,
        exit_code: int | None = 0,
        stdout: bytes = b"",
        stderr: str = "",
        timed_out: bool = False,
        truncated: bool = False,
    ) -> None:
        self.git[machine_id] = {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout": _b64(stdout),
            "stderr": stderr,
            "truncated": truncated,
        }

    def fail(
        self, machine_id: str, code: str, stage: Literal["none", "partial"] | None = None
    ) -> None:
        self.failures[machine_id] = (code, stage)

    def recover(self, machine_id: str) -> None:
        self.failures.pop(machine_id, None)

    async def send(
        self, machine_id: str, command: str, payload: dict[str, Any], timeout_ms: int = 30000
    ) -> dict[str, Any]:
        self.requests.append((machine_id, command, payload, timeout_ms))
        if machine_id in self.failures:
            code, stage = self.failures[machine_id]
            raise NodeCommandError(code, f"scripted {code}", stage)
        query = payload["query"]
        files = None
        git = None
        if query["kind"] == "files":
            machine_files = self.files[machine_id]
            files = [machine_files[path] for path in query["paths"]]
        else:
            git = self.git[machine_id]
        return {
            "root": "/checkout",
            "project_id": _PROJECT_ID,
            "home": _HOME,
            "temp_dir": _TEMP_DIR,
            "files": files,
            "git": git,
        }

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Route the seam's node client to this fake and pin the hub's machine id."""
        monkeypatch.setattr(checkout_reads, "node_command_client", lambda: self)
        monkeypatch.setattr(checkout_reads, "get_machine_id", lambda: HUB_MACHINE)
