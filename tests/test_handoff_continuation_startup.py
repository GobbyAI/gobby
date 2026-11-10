"""Exercise handoff startup and failed-start diagnostics without provider CLIs."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from tests.e2e import handoff_continuation_fixture as harness
from tests.e2e.composer_proof import ProofRefused
from tests.e2e.composer_proof_live import LiveProof
from tests.e2e.composer_proof_trace import ProofTrace
from tests.e2e.conftest import DaemonHealthTimeoutError, wait_for_daemon_health
from tests.e2e.handoff_continuation_admission import Admission
from tests.e2e.handoff_continuation_barrier import HandoffBarrier

pytestmark = pytest.mark.unit
FATAL = "Fatal error: native deployment runtime authority is unavailable"


@dataclass
class FailedStart:
    command: list[str]
    env: dict[str, str]
    diagnostics: str
    artifact: dict[str, Any]
    artifact_dir: Path
    project: Path


@pytest.fixture
async def failed_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FailedStart]:
    project = tmp_path / "project"
    home = project / "home"
    logs = home / "logs"
    logs.mkdir(parents=True)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)
    native = tmp_path / "native"
    native.mkdir()
    (native / "gdaemon").touch()
    (home / "bootstrap.yaml").write_text("secret bootstrap must not be retained")
    (logs / "credential-link.log").symlink_to(home / "bootstrap.yaml")
    root = tmp_path / "sockets"
    root.mkdir()
    spec = Admission.model_construct(
        grant="PD_AUTHORIZED_ISOLATED_EXECUTION",
        reviewed_commit="a" * 40,
        reviewed_tree="b" * 40,
        issued_at=datetime.now(UTC),
        exclusion_message_id=UUID(int=100),
        complete_exclusion_map=True,
        excluded_identities=frozenset(UUID(int=index) for index in range(26)),
        native={},
        providers={},
        support_tools=[],
        artifact_dir=artifacts,
        worktree_test_notice_id=UUID(int=101),
    )
    monkeypatch.setattr(harness, "verify_binary_set", lambda spec: native)
    monkeypatch.setattr(harness, "require_private_root", lambda path: None)
    monkeypatch.setattr(harness, "copy_daemon_api_key", lambda source, target: None)
    monkeypatch.setattr(harness, "ConfigMutations", Mock())
    monkeypatch.setattr(harness, "sealed_environment", lambda env, **kwargs: {})
    monkeypatch.setattr(
        harness, "prepare_daemon_env", lambda **kwargs: {"GOBBY_NATIVE_BIN_DIR": str(native)}
    )
    monkeypatch.setattr(harness, "binary_readback", lambda *args: [])
    monkeypatch.setattr(harness, "require_provider_trust", lambda *args, **kwargs: None)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kwargs: str(root))
    monkeypatch.setattr(ProofTrace, "start", AsyncMock())
    monkeypatch.setattr(ProofTrace, "close", AsyncMock())
    process = Mock(pid=123456)
    process.poll.return_value = 1
    launch = Mock(return_value=process)
    diagnostics: list[str] = []

    def start(*args: Any, **kwargs: Any) -> Mock:
        kwargs["stderr"].write(FATAL.encode())
        kwargs["stderr"].flush()
        launch(*args, **kwargs)
        return process

    def unhealthy(port: int, **kwargs: Any) -> None:
        try:
            wait_for_daemon_health(port, **kwargs, timeout=0, min_attempts=0)
        except DaemonHealthTimeoutError as exc:
            diagnostics.append(str(exc))
            raise

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(harness, "wait_for_daemon_health", unhealthy)
    fixture_function = cast(
        Callable[..., AsyncIterator[tuple[LiveProof, HandoffBarrier]]],
        harness.handoff_fixture._get_wrapped_function(),
    )
    fixture = fixture_function(
        spec,
        {"GOBBY_DAEMON_URL": "http://127.0.0.1:1"},
        "codex",
        "compact",
        project,
        (home / "bootstrap.yaml", 1, 2),
        Mock(),
    )
    with pytest.raises(ProofRefused, match="partial daemon startup"):
        await anext(fixture)
    artifact = json.loads(next(artifacts.glob("handoff-proof-*.json")).read_text())
    yield FailedStart(
        launch.call_args.args[0],
        launch.call_args.kwargs["env"],
        "\n".join(diagnostics),
        artifact,
        artifacts,
        project,
    )


async def test_runner_is_dispatched_through_native_supervisor(failed_start: FailedStart) -> None:
    assert Path(failed_start.command[0]).name == "gdaemon"
    assert failed_start.command[1:] == ["serve"]
    backend = json.loads(failed_start.env["GOBBY_BACKEND_COMMAND"])
    assert backend[1:] == ["-m", "tests.e2e.handoff_continuation_runner"]


async def test_health_timeout_reports_real_stderr(failed_start: FailedStart) -> None:
    assert FATAL in failed_start.diagnostics


async def test_logs_survive_project_teardown_without_copying_state(
    failed_start: FailedStart,
) -> None:
    shutil.rmtree(failed_start.project)
    retained = failed_start.artifact.get("retained_logs")
    assert retained, "artifact must reference durable log copies"
    log_root = Path(retained)
    assert log_root.is_relative_to(failed_start.artifact_dir)
    assert FATAL in (log_root / "daemon_error.log").read_text()
    assert log_root.stat().st_mode & 0o777 == 0o700
    assert (log_root / "daemon_error.log").stat().st_mode & 0o777 == 0o600
    assert not (log_root / "credential-link.log").exists()
    assert not list(failed_start.artifact_dir.rglob("bootstrap.yaml"))
    assert "home" not in (failed_start.artifact.get("isolated_state_retained") or {})
