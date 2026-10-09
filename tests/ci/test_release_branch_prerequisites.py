"""Release CI must provision the tools and isolated service used by pre-push."""

import os
import socket
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import redis
import yaml

_FALKOR_KEYS = (
    "GOBBY_TEST_FALKOR_HOST",
    "GOBBY_TEST_FALKOR_PORT",
    "GOBBY_TEST_FALKOR_PASSWORD",
)


def _falkor_harness(
    repo_root: Path, tmp_path: Path, settings: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    script = (repo_root / "pre-push-test.sh").read_text()
    start = script.index("read_managed_falkordb_settings() {")
    end = script.index("\n# bash 3.2", start)
    harness = tmp_path / "falkor.sh"
    harness.write_text(
        "set -euo pipefail\n"
        "uv_run() { printf 'managed-host\\n16379\\nmanaged-test-password\\n'; }\n"
        + script[start:end]
        + "\nload_pytest_falkordb_settings\n"
        + 'printf \'%s\\n\' "$PYTEST_FALKORDB_HOST" "$PYTEST_FALKORDB_PORT" "$PYTEST_FALKORDB_PASSWORD"\n'
    )
    env = {key: value for key, value in os.environ.items() if key not in _FALKOR_KEYS}
    env.update(settings)
    env["HOME"] = str(tmp_path)
    return subprocess.run(
        ["bash", str(harness)], env=env, capture_output=True, text=True, check=False, timeout=10
    )


def test_pre_push_uses_complete_isolated_falkor_settings(repo_root: Path, tmp_path: Path) -> None:
    values = ("127.0.0.1", "26379", "test password * literal")
    result = _falkor_harness(repo_root, tmp_path, dict(zip(_FALKOR_KEYS, values, strict=True)))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == list(values)
    assert result.stderr == ""


@pytest.mark.parametrize("missing", _FALKOR_KEYS)
def test_pre_push_rejects_partial_isolated_falkor_settings(
    repo_root: Path, tmp_path: Path, missing: str
) -> None:
    settings = dict(zip(_FALKOR_KEYS, ("127.0.0.1", "26379", "test-password"), strict=True))
    del settings[missing]
    result = _falkor_harness(repo_root, tmp_path, settings)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "Set all GOBBY_TEST_FALKOR_HOST" in result.stderr
    assert "test-password" not in result.stderr


def test_pre_push_retains_managed_falkor_fallback(repo_root: Path, tmp_path: Path) -> None:
    result = _falkor_harness(repo_root, tmp_path, {})
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["managed-host", "16379", "managed-test-password"]


def test_release_workflow_provisions_pre_push_tools_and_falkor(repo_root: Path) -> None:
    workflow: dict[str, Any] = yaml.safe_load(
        (repo_root / ".github/workflows/postgres-pgsearch-smoke.yml").read_text()
    )
    job = workflow["jobs"]["release-branch-ci"]
    steps = job["steps"]
    names = [step.get("name", "") for step in steps]
    pre_push_index = names.index("Run pre-push suite")
    for name in (
        "Install Rust toolchain",
        "Install cargo-nextest",
        "Setup Zig 0.16.0",
        "Start isolated FalkorDB test service",
        "Build Python test native helpers",
    ):
        assert names.index(name) < pre_push_index
    nextest = steps[names.index("Install cargo-nextest")]
    assert nextest["with"]["tool"] == "nextest"
    service = steps[names.index("Start isolated FalkorDB test service")]["run"]
    assert "GOBBY_TEST_FALKOR_PASSWORD=" in service
    assert "::add-mask::" in service
    assert "docker exec falkordb-test redis-cli PING" in service
    assert "--tmpfs /var/lib/falkordb/data" in service
    assert job["env"]["GOBBY_TEST_FALKOR_HOST"] == "127.0.0.1"
    assert job["env"]["GOBBY_TEST_FALKOR_PORT"] == "16379"
    cleanup = steps[names.index("Remove isolated test services")]
    assert cleanup["if"] == "always()"
    assert "docker rm -f falkordb-test" in cleanup["run"]


def test_pre_push_shell_syntax(repo_root: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(repo_root / "pre-push-test.sh")],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("ci", ["true", "false"])
def test_live_agy_probe_stays_local_and_is_named_in_ci_report(
    repo_root: Path, tmp_path: Path, ci: str
) -> None:
    script = (repo_root / "pre-push-test.sh").read_text()
    header = script[script.index("FAILED=0") : script.index("uv_run() {")]
    harness = tmp_path / "selection.sh"
    harness.write_text(
        "set -euo pipefail\n"
        + header
        + 'printf \'%s\\n\' "$PYTEST_AGY_PROBE" "${PYTEST_SELECTION_ARGS[@]}" "${PYTEST_FORBIDDEN_SKIP_REASONS[@]}"\n'
    )
    result = subprocess.run(
        ["bash", str(harness)],
        env=os.environ | {"CI": ci},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout.splitlines()
    if ci == "true":
        assert output[0] == "0"
        assert "--ignore=tests/ai/test_agy_probe.py" in output
        assert "set GOBBY_RUN_AGY_PROBE=1" not in output
    else:
        assert output[0] == "1"
        assert "--ignore=tests/ai/test_agy_probe.py" not in output
        assert "set GOBBY_RUN_AGY_PROBE=1" in output
    assert "Live AGY probe (tests/ai/test_agy_probe.py): not run in CI" in script
    assert 'record_skipped_command "live-agy-probe" "non-gating"' in script
    assert '[ "$PYTEST_AGY_PROBE" -eq 1 ] && ! command -v agy' in script


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("GOBBY_RUN_RELEASE_CI_SMOKE") != "1",
    reason="set GOBBY_RUN_RELEASE_CI_SMOKE=1 for the isolated Docker service smoke",
)
def test_release_workflow_falkor_service_supports_authenticated_graphs(
    repo_root: Path, tmp_path: Path
) -> None:
    workflow: dict[str, Any] = yaml.safe_load(
        (repo_root / ".github/workflows/postgres-pgsearch-smoke.yml").read_text()
    )
    steps = workflow["jobs"]["release-branch-ci"]["steps"]
    service = next(
        step for step in steps if step.get("name") == "Start isolated FalkorDB test service"
    )
    container = f"gobby-release-ci-test-{uuid.uuid4().hex}"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    github_env = tmp_path / "github-env"
    startup = tmp_path / "start-falkor.sh"
    startup.write_text("set -euo pipefail\n" + service["run"].replace("falkordb-test", container))
    env = os.environ | {
        "GITHUB_ENV": str(github_env),
        "GOBBY_TEST_FALKOR_PORT": str(port),
    }
    try:
        result = subprocess.run(
            ["bash", str(startup)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        password = github_env.read_text().strip().split("=", 1)[1]
        # Startup stdout contains the masking command; only show redacted stderr.
        assert result.returncode == 0, result.stderr.replace(password, "[redacted]")
        client = redis.Redis(host="127.0.0.1", port=port, password=password, socket_timeout=5)
        execute = cast(Callable[..., object], client.execute_command)
        try:
            assert client.ping()
            execute("GRAPH.QUERY", "release_ci_test", "CREATE (:Smoke {value: 42})")
            response = execute("GRAPH.QUERY", "release_ci_test", "MATCH (n:Smoke) RETURN n.value")
            assert isinstance(response, list)
            assert response[1] == [[42]]
            execute("GRAPH.DELETE", "release_ci_test")
        finally:
            client.close()
        unauthenticated = redis.Redis(host="127.0.0.1", port=port, socket_timeout=5)
        try:
            with pytest.raises(redis.exceptions.AuthenticationError):
                unauthenticated.ping()
        finally:
            unauthenticated.close()
    finally:
        cleanup = subprocess.run(
            ["docker", "rm", "-f", container],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert cleanup.returncode == 0, "isolated FalkorDB cleanup failed"
