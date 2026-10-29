"""Opt-in reviewer proofs against an isolated copy of the pinned host SRT."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from gobby.agents import srt_monitor
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.srt_runtime import (
    SandboxLaunch,
    make_srt_installation_immutable,
    prepare_sandbox_launch,
    write_srt_content_manifest,
)
from gobby.utils.dependency_requirements import SRT_RELEASE
from tests.srt_fixture_helpers import write_srt_proxy_fixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "darwin" or os.environ.get("GOBBY_RUN_SRT_HOST_TESTS") != "1",
        reason="requires an explicit unsandboxed Darwin host proof",
    ),
]


def _make_writable(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | (0o700 if path.is_dir() else 0o200))


@pytest.fixture
def reviewer_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[SandboxLaunch, Path, dict[str, str]]]:
    source = os.environ.get("GOBBY_SRT_TEST_ROOT")
    if source is None:
        pytest.fail("GOBBY_SRT_TEST_ROOT must identify the installed pinned version")
    probe = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", "(version 1) (allow default)", "/usr/bin/true"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert probe.returncode == 0, (
        "Run this proof from the host, not nested Seatbelt: " + probe.stderr
    )
    home = tmp_path / "gobby-home"
    runtime = home / "tools" / "srt" / SRT_RELEASE.version
    shutil.copytree(source, runtime, symlinks=True)
    _make_writable(runtime)
    write_srt_proxy_fixture(runtime)
    runner = Path(__file__).parents[3] / "src" / "gobby" / "agents" / "srt_runner.mjs"
    shutil.copyfile(runner, runtime / "runner.mjs")
    (runtime / "receipt.json").write_text(json.dumps(SRT_RELEASE.receipt_fields()))
    write_srt_content_manifest(runtime)
    make_srt_installation_immutable(runtime)
    monkeypatch.setenv("GOBBY_HOME", str(home))
    # Synthetic owner configuration stays unreadable to the sandboxed hook path.
    (home / "bootstrap.yaml").write_text("daemon_port: 12345\n")
    (home / "bootstrap.yaml").chmod(0o600)
    workspace = Path(os.environ.get("GOBBY_REVIEWER_PROOF_WORKSPACE", str(tmp_path / "workspace")))
    workspace.mkdir(exist_ok=True)
    if not (workspace / "pyproject.toml").exists():
        (workspace / "pyproject.toml").write_text(
            '[project]\nname="reviewer-proof"\nversion="0.0.0"\nrequires-python=">=3.13"\n'
        )
    environment = dict(os.environ)
    environment["GOBBY_DAEMON_URL"] = "http://127.0.0.1:1"
    before = len(srt_monitor._children)
    try:
        with tempfile.TemporaryDirectory(prefix="gsrt-proof-") as monitor_directory:
            monkeypatch.setattr(srt_monitor, "srt_mux_tmpdir", lambda: Path(monitor_directory))
            launch = asyncio.run(
                prepare_sandbox_launch(
                    config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
                    provider="claude",
                    workspace_path=str(workspace),
                    run_id="reviewer-host-proof",
                    resolver=None,
                    daemon_port=1,
                    websocket_port=2,
                    api_base=None,
                    env=environment,
                    prewarm_pre_commit_store=False,
                )
            )
            _, environment = launch.compose_subprocess([], environment)
            yield launch, workspace, environment
    finally:
        for monitor in srt_monitor._children[before:]:
            monitor.terminate()
            try:
                monitor.wait(timeout=5)
            except subprocess.TimeoutExpired:
                monitor.kill()
                monitor.wait(timeout=5)
        _make_writable(runtime)


def _run(
    launch: SandboxLaunch,
    workspace: Path,
    environment: dict[str, str],
    command: list[str],
    *,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    argv = launch.wrap(command)
    argv[argv.index("--") + 1] = command[0]
    return subprocess.run(
        argv,
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def test_reviewer_cached_settings_and_python_environment(
    reviewer_launch: tuple[SandboxLaunch, Path, dict[str, str]],
) -> None:
    launch, workspace, environment = reviewer_launch
    settings = Path(environment["XDG_CACHE_HOME"]) / "claude" / "settings.json"
    # The retired close-reviewer launch copied protected controls into this cache.
    # Current launches preserve the caller's config selector and make no private copy.
    assert "CLAUDE_CONFIG_DIR" not in launch.provider_env
    assert not settings.exists()
    settings.parent.mkdir(parents=True)
    settings.write_text("{}")
    uv = shutil.which("uv")
    assert uv is not None
    script = (
        "import json, os, subprocess, sys; from pathlib import Path; "
        "p=Path(os.environ['XDG_CACHE_HOME'])/'claude'/'settings.json'; "
        "t=p.with_suffix('.tmp'); t.write_text('{\"updated\":true}'); t.replace(p); "
        "assert json.loads(p.read_text())['updated']; p.unlink(); "
        "from gobby.utils.daemon_url import load_client_bootstrap; "
        "from gobby.config.bootstrap import load_bootstrap; "
        "assert load_client_bootstrap(load_bootstrap).daemon_port == 1; "
        "from gobby.hooks.factory import HookManagerFactory; "
        "from gobby.config.bootstrap import BootstrapConfigError\n"
        "try:\n"
        "    HookManagerFactory._resolve_config(None, None)\n"
        "except BootstrapConfigError as error:\n"
        "    assert 'database_url' in str(error), str(error)\n"
        "else:\n"
        "    raise AssertionError('hook config silently fell back')\n"
        "r=subprocess.run([sys.argv[1], 'run', '--no-sync', '--python', sys.executable, "
        "'python', '-c', 'import sys; print(sys.prefix)'], capture_output=True, text=True); "
        "assert r.returncode == 0, r.stderr; "
        "assert r.stdout.strip() == os.environ['UV_PROJECT_ENVIRONMENT'], r.stdout; "
        "print('cached-settings-and-endpoint-and-uv-ok')"
    )
    result = _run(launch, workspace, environment, [sys.executable, "-c", script, uv])
    assert result.returncode == 0, result.stderr
    assert "cached-settings-and-endpoint-and-uv-ok" in result.stdout
    assert Path(environment["UV_PROJECT_ENVIRONMENT"]).is_dir()


def test_reviewer_workspace_case_alias_still_needs_hook_scope(
    reviewer_launch: tuple[SandboxLaunch, Path, dict[str, str]],
) -> None:
    launch, workspace, environment = reviewer_launch
    alias = workspace.with_name(workspace.name.upper())
    if not alias.exists():
        pytest.skip("Filesystem distinguishes workspace casing")
    assert alias.samefile(workspace)
    script = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('case-alias-ok')"
    with tempfile.NamedTemporaryFile(prefix="case-alias-probe-", dir=workspace) as owned_file:
        probe = Path(owned_file.name)
        result = _run(
            launch,
            workspace,
            environment,
            [sys.executable, "-c", script, str(alias / probe.name)],
        )
        assert result.returncode == 0, result.stderr
        assert probe.read_text() == "case-alias-ok"
        print("Managed SRT allowed the harmless write through the granted workspace case alias")


def test_reviewer_diff_process_substitution(
    reviewer_launch: tuple[SandboxLaunch, Path, dict[str, str]],
) -> None:
    launch, workspace, environment = reviewer_launch
    cat = _run(
        launch,
        workspace,
        environment,
        ["/bin/bash", "-c", "cat <(printf 'a\\n') <(printf 'b\\n')"],
    )
    assert cat.returncode == 0, cat.stderr
    assert cat.stdout == "a\nb\n"
    diff = _run(
        launch,
        workspace,
        environment,
        ["/bin/bash", "-c", "/usr/bin/diff <(printf 'a\\n') <(printf 'a\\n')"],
    )
    assert diff.returncode == 0, diff.stderr


@pytest.mark.timeout(1200)
def test_reviewer_rust_compare(
    reviewer_launch: tuple[SandboxLaunch, Path, dict[str, str]],
) -> None:
    if "GOBBY_REVIEWER_PROOF_WORKSPACE" not in os.environ:
        pytest.skip("set GOBBY_REVIEWER_PROOF_WORKSPACE to the task checkout for the Rust proof")
    launch, workspace, environment = reviewer_launch
    cargo_home = Path(environment["CARGO_HOME"])
    source_home = Path(os.environ.get("CARGO_HOME", str(Path.home() / ".cargo")))
    (cargo_home / "registry").symlink_to(source_home / "registry", target_is_directory=True)
    environment.update(CARGO_BUILD_JOBS="3", CARGO_NET_OFFLINE="true", RUSTC_WRAPPER="")
    result = _run(
        launch,
        workspace,
        environment,
        ["cargo", "nextest", "run", "--offline", "-p", "gobby-daemon", "--test", "compare"],
        timeout=1100,
    )
    assert result.returncode == 0, result.stderr[-20000:]
    assert "17 passed" in result.stderr
    assert launch.violation_path is not None
    operations = [
        json.loads(line).get("line", "").splitlines()[0].partition(" deny(1) ")[2]
        for line in Path(launch.violation_path).read_text().splitlines()
    ]
    assert not any("file-write-" in operation for operation in operations), operations
