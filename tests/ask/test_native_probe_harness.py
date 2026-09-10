from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import psycopg
import pytest

from gobby.ask.runtime_validation import (
    ASK_NATIVE_PROBE_EXPECTATIONS,
    ask_runtime_control_digest,
    load_ask_runtime_validation,
    load_ask_runtime_validation_artifacts,
)
from tests.ask import native_probe_harness as harness

pytestmark = pytest.mark.unit
_TEST_DATABASE_URL = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"


def _drive_arguments(*, database_url: str, daemon_pid: int = 4242) -> argparse.Namespace:
    return argparse.Namespace(
        daemon_url="http://127.0.0.1:19472",
        database_url=database_url,
        daemon_pid=daemon_pid,
    )


def _provider(path: Path) -> Path:
    path.write_text("#!/bin/sh\necho 'Claude Code 2.1.265'\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _observations(executable: Path) -> list[dict[str, Any]]:
    resolved = str(executable.resolve())
    executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    control_digest = ask_runtime_control_digest("claude", "claude.ai")
    observations: list[dict[str, Any]] = []
    for phase in ("fresh", "resumed"):
        agent_run_id = f"{phase}-agent-run"
        for case, expected in ASK_NATIVE_PROBE_EXPECTATIONS.items():
            record: dict[str, Any] = {
                "phase": phase,
                "case": case,
                "observed": expected,
                "agent_run_id": agent_run_id,
                "session_id": f"{phase}-session",
                "terminal_id": f"{phase}-terminal",
                "process_id": 1001 if phase == "fresh" else 1002,
                "provider_executable": resolved,
                "provider_executable_sha256": executable_sha256,
                "provider_version": "2.1.265",
                "auth_mode": "claude.ai",
                "control_digest": control_digest,
                "policy_hash": ("b" if phase == "fresh" else "c") * 64,
            }
            if phase == "resumed":
                record["resumed_from_agent_run_id"] = "fresh-agent-run"
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            observations.append(
                {
                    "phase": phase,
                    "case": case,
                    "observed": expected,
                    "receipt": {
                        "source": (
                            "native_runtime"
                            if case in {"native_shell", "native_edit", "unrestricted_read", "web"}
                            else "mcp_response"
                        ),
                        "record": record,
                        "sha256": hashlib.sha256(encoded).hexdigest(),
                    },
                }
            )
    return observations


class _Cursor:
    def __init__(
        self,
        *,
        row: dict[str, object] | None = None,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.row = row
        self.rows = rows or []

    def fetchone(self) -> dict[str, object] | None:
        return self.row

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class _Connection:
    def __init__(
        self,
        *,
        pipeline: dict[str, object],
        agents: list[dict[str, object]],
    ) -> None:
        self.pipeline = pipeline
        self.agents = agents
        self.parameters: list[tuple[object, ...]] = []

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, parameters: tuple[object, ...]) -> _Cursor:
        self.parameters.append(parameters)
        if "FROM pipeline_executions" in query:
            return _Cursor(row=self.pipeline)
        return _Cursor(rows=self.agents)


def test_probe_isolation_rejects_remote_database_with_test_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://gobby_test:gobby_test@db.example/gobby_test",
    )
    arguments = _drive_arguments(
        database_url="postgresql://gobby_test:gobby_test@db.example/gobby_test"
    )

    with pytest.raises(ValueError, match="loopback test database"):
        harness._assert_isolated(arguments)


@pytest.mark.parametrize("daemon_pid", [-1, 0, os.getpid(), os.getppid()])
def test_probe_isolation_rejects_unsafe_daemon_pid(
    daemon_pid: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    arguments = _drive_arguments(
        database_url=_TEST_DATABASE_URL,
        daemon_pid=daemon_pid,
    )

    with pytest.raises(ValueError, match="safe isolated daemon PID"):
        harness._assert_isolated(arguments)


def test_probe_isolation_rejects_restart_database_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://operator@127.0.0.1:60887/gobby")
    arguments = _drive_arguments(database_url=_TEST_DATABASE_URL)

    with pytest.raises(ValueError, match="DATABASE_URL must match"):
        harness._assert_isolated(arguments)


def test_wait_for_first_agent_requires_a_running_native_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_checks: list[str] = []

    monkeypatch.setattr(harness, "_ask_run_ids", lambda *_args: ["agent-run"])

    def agent_is_live(_database_url: str, agent_run_id: str) -> bool:
        live_checks.append(agent_run_id)
        return len(live_checks) == 2

    monkeypatch.setattr(harness, "_agent_is_live", agent_is_live, raising=False)
    monkeypatch.setattr("tests.ask.native_probe_harness.time.sleep", lambda _seconds: None)

    assert harness._wait_for_first_agent("database", "ask-run", time.monotonic() + 1) == (
        "agent-run"
    )
    assert live_checks == ["agent-run", "agent-run"]


def test_wait_for_terminal_fails_when_deadline_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        is_success = True

        @staticmethod
        def json() -> dict[str, str]:
            return {"status": "running"}

    class _Client:
        @staticmethod
        def get(*_args: object, **_kwargs: object) -> _Response:
            return _Response()

    moments = iter([1.0, 2.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError, match="did not finish"):
        harness._wait_for_terminal(
            cast("httpx.Client", _Client()),
            ask_run_id="ask-run",
            project_id="project",
            deadline=2.0,
        )


def test_drive_completes_fresh_and_separately_resumed_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        def __init__(self, run_id: str) -> None:
            self.run_id = run_id

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"run_id": self.run_id}

    class _Client:
        posts: list[dict[str, object]] = []

        def __init__(self, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, _path: str, *, json: dict[str, object], timeout: int) -> _Response:
            self.posts.append(json)
            return _Response(f"ask-{len(self.posts)}")

    exported_dirs: list[Path] = []

    def export(_database_url: str, ask_run_id: str, output_dir: Path) -> dict[str, object]:
        exported_dirs.append(output_dir)
        if ask_run_id == "ask-1":
            return {"agent_runs": [{"id": "fresh-run"}]}
        return {
            "agent_runs": [
                {"id": "interrupted-run"},
                {"id": "resumed-run"},
            ]
        }

    monkeypatch.setattr(harness, "_assert_isolated", lambda _arguments: None)
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(
        harness,
        "_wait_for_terminal",
        lambda *_args, **_kwargs: {"status": "completed"},
    )
    monkeypatch.setattr(harness, "_wait_for_first_agent", lambda *_args: "interrupted-run")
    monkeypatch.setattr(
        harness,
        "_restart_isolated_daemon",
        lambda *_args: SimpleNamespace(pid=5252),
    )
    monkeypatch.setattr(harness, "_wait_for_health", lambda *_args: None)
    monkeypatch.setattr(harness, "_export_raw", export)
    arguments = argparse.Namespace(
        daemon_url="http://127.0.0.1:19472",
        database_url=_TEST_DATABASE_URL,
        project_id="project",
        caller_session_id="caller",
        bearer_token=None,
        daemon_pid=4242,
        restart_command_json=tmp_path / "restart.json",
        daemon_cwd=tmp_path,
        output_dir=tmp_path / "probe",
        timeout_seconds=600.0,
    )

    assert harness._drive(arguments) == 0

    assert len(_Client.posts) == 2
    assert exported_dirs == [arguments.output_dir / "fresh", arguments.output_dir / "resumed"]


def test_raw_export_hashes_database_rows_and_transcript_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = tmp_path / "native.jsonl"
    transcript_payload = b'{"type":"result","result":"blocked"}\n'
    transcript.write_bytes(transcript_payload)
    connection = _Connection(
        pipeline={"id": "ask-run", "status": "completed"},
        agents=[
            {"id": "fresh-run", "created_at": "1", "transcript_path": str(transcript)},
            {"id": "resumed-run", "created_at": "2", "transcript_path": None},
        ],
    )
    monkeypatch.setattr(
        harness,
        "_ask_run_ids",
        lambda _database_url, _ask_run_id: ["fresh-run", "resumed-run"],
    )
    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: connection)

    output_dir = tmp_path / "export"
    exported = harness._export_raw("postgresql://unused", "ask-run", output_dir)

    raw_payload = (output_dir / "raw-probe.json").read_bytes()
    assert (output_dir / "raw-probe.sha256").read_text(encoding="utf-8").strip() == (
        hashlib.sha256(raw_payload).hexdigest()
    )
    assert exported["transcripts"] == [
        {
            "path": "transcript-0.jsonl",
            "sha256": hashlib.sha256(transcript_payload).hexdigest(),
        }
    ]
    assert connection.parameters == [
        ("ask-run",),
        (["fresh-run", "resumed-run"],),
    ]


def test_seal_writes_manifest_accepted_by_production_loader(tmp_path: Path) -> None:
    provider = _provider(tmp_path / "claude")
    observations = tmp_path / "observations.json"
    observations.write_text(json.dumps(_observations(provider)), encoding="utf-8")
    artifact = tmp_path / "runtime" / "claude.json"
    manifest = tmp_path / "runtime" / "manifest.json"
    arguments = argparse.Namespace(
        provider="claude",
        provider_executable=provider,
        auth_mode="claude.ai",
        observations=observations,
        output=artifact,
        manifest=manifest,
        profile=["ask-investigator", "ask-reviewer"],
    )

    assert harness._seal(arguments) == 0

    loaded = load_ask_runtime_validation_artifacts(manifest)
    assert set(loaded) == {"ask-investigator", "ask-reviewer"}
    validation = load_ask_runtime_validation(
        loaded["ask-investigator"], provider_executable=provider
    )
    assert validation.verified_artifact is True
