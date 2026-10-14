"""The shared start preflight that restart and cutover run before acting."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from gobby.cli import cli, daemon_preflight, install_setup_srt
from gobby.cli.runtime import CliRuntime
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import load_bootstrap
from gobby.install.bin_set_coherence import BinarySetCoherenceError
from gobby.storage import schema_divergence
from gobby.storage.schema_contract import SchemaContractError, expected_schema_identity

pytestmark = pytest.mark.unit

daemon_module = importlib.import_module("gobby.cli.daemon_lifecycle")
daemon_start = importlib.import_module("gobby.cli.daemon_start")


def test_restart_refuses_node_bootstrap(
    tmp_path: Path, plan_calls: list[tuple[str, Path | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    bootstrap_path.write_text(
        "datastore_mode: remote\n"
        "hub_daemon_url: http://hub.example.test:60887\n"
        "database_url: postgresql://gobby:secret@100.64.0.10:5432/gobby\n",
        encoding="utf-8",
    )
    bootstrap_path.chmod(0o600)
    monkeypatch.setattr(
        daemon_preflight, "load_bootstrap", lambda: load_bootstrap(str(bootstrap_path))
    )
    launched: list[str] = []

    def forbid(name: str) -> Callable[..., None]:
        def forbidden(*_args: object, **_kwargs: object) -> None:
            launched.append(name)
            raise AssertionError(f"a node restart must not reach {name}")

        return forbidden

    monkeypatch.setattr(daemon_module, "_do_stop", forbid("stop"))
    monkeypatch.setattr(daemon_module, "_restart_backend", forbid("backend"))
    monkeypatch.setattr(daemon_start, "_services_start", forbid("services"))
    monkeypatch.setattr(daemon_start, "start", click.Command("start", callback=forbid("start")))
    result = CliRunner().invoke(cli, ["restart"])
    assert result.exit_code == 1, result.output
    assert (
        "Refusing to restart: datastore_mode: remote is a node; a node runs no Python "
        "daemon and never takes the hub lease. See docs/guides/shared-stack.md (Client setup)."
    ) in result.output
    assert "The running daemon was left alone." in result.output
    assert launched == []
    assert plan_calls == []


_CANDIDATE = Path("/workspace/target/release/gdaemon")
_BOOTSTRAP_URL = "postgresql://bootstrap.example/gobby"


@pytest.fixture
def ctx() -> click.Context:
    return click.Context(click.Command("restart"), obj=CliRuntime(config_file=None))


@pytest.fixture
def plan_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path | None]]:
    """Record every schema plan the preflight runs."""
    calls: list[tuple[str, Path | None]] = []

    def plan_schema(database_url: str, *, gdaemon: Path | None = None) -> str:
        calls.append((database_url, gdaemon))
        return "schema gobby plan: database v1, code v1, baseline_pending=false, pending=0 []"

    monkeypatch.setattr(daemon_preflight, "plan_schema", plan_schema)
    return calls


@pytest.fixture(autouse=True)
def _every_gate_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default each gate to "no refusal" so a test states only what it exercises."""
    monkeypatch.setattr(daemon_preflight, "worktree_daemon_refusal", lambda: None)
    monkeypatch.setattr(
        daemon_preflight, "dirty_bundled_content_refusal", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(schema_divergence, "binary_set_apply_refusal", lambda *_a, **_k: None)
    monkeypatch.setattr(schema_divergence, "schema_apply_refusal", lambda _database: None)
    monkeypatch.setattr(daemon_preflight, "_open_hub", lambda _ctx: None)
    monkeypatch.setattr(daemon_preflight, "verify_catalog_test_cluster", lambda _url: None)
    monkeypatch.setattr(install_setup_srt, "restage_srt_runner", lambda: None)
    config = DaemonConfig()
    config.agent_sandbox.enabled = False
    config.web_chat_sandbox.enabled = False
    monkeypatch.setattr(CliRuntime, "read_only_operational_config", lambda _runtime: config)
    monkeypatch.setattr(
        daemon_preflight,
        "load_bootstrap",
        lambda: SimpleNamespace(datastore_mode="local", database_url=_BOOTSTRAP_URL),
    )


@pytest.mark.parametrize("candidate", [None, _CANDIDATE], ids=["restart", "cutover"])
@pytest.mark.parametrize("sandbox", ["agent_sandbox", "web_chat_sandbox"])
def test_runner_checksum_mismatch_refuses_before_schema_plan(
    monkeypatch: pytest.MonkeyPatch,
    ctx: click.Context,
    plan_calls: list[tuple[str, Path | None]],
    candidate: Path | None,
    sandbox: str,
) -> None:
    from gobby.agents import srt_runtime

    config = DaemonConfig()
    config.agent_sandbox.enabled = sandbox == "agent_sandbox"
    config.web_chat_sandbox.enabled = sandbox == "web_chat_sandbox"
    monkeypatch.setattr(CliRuntime, "read_only_operational_config", lambda _runtime: config)
    monkeypatch.setattr(daemon_preflight, "_candidate_identity_refusal", lambda *_a, **_k: None)

    def checksum_mismatch() -> None:
        raise srt_runtime.SrtRuntimeError("SRT runner checksum does not match the pinned release")

    monkeypatch.setattr(srt_runtime, "verify_srt_installation", checksum_mismatch)

    refusal = daemon_preflight.restart_start_refusal(ctx, candidate)

    assert refusal == (
        "Managed SRT sandbox preflight failed: "
        "SRT runner checksum does not match the pinned release"
    )
    assert plan_calls == []


@pytest.mark.parametrize("candidate", [None, _CANDIDATE], ids=["restart", "cutover"])
@pytest.mark.parametrize("sandbox", ["agent_sandbox", "web_chat_sandbox"])
def test_restart_restages_runner_before_verification(
    monkeypatch: pytest.MonkeyPatch,
    ctx: click.Context,
    plan_calls: list[tuple[str, Path | None]],
    candidate: Path | None,
    sandbox: str,
) -> None:
    from gobby.agents import srt_runtime

    config = DaemonConfig()
    config.agent_sandbox.enabled = sandbox == "agent_sandbox"
    config.web_chat_sandbox.enabled = sandbox == "web_chat_sandbox"
    monkeypatch.setattr(CliRuntime, "read_only_operational_config", lambda _runtime: config)
    monkeypatch.setattr(daemon_preflight, "_candidate_identity_refusal", lambda *_a, **_k: None)
    events: list[str] = []

    def restage() -> None:
        events.append("restage")

    def verify() -> None:
        if events != ["restage"]:
            raise srt_runtime.SrtRuntimeError("stale runner receipt")
        events.append("verify")

    monkeypatch.setattr(install_setup_srt, "restage_srt_runner", restage)
    monkeypatch.setattr(srt_runtime, "verify_srt_installation", verify)

    assert daemon_preflight.restart_start_refusal(ctx, candidate) is None
    assert events == ["restage", "verify"]
    assert plan_calls == [(_BOOTSTRAP_URL, candidate)]


def test_failed_runner_restage_refuses_before_schema_plan(
    monkeypatch: pytest.MonkeyPatch,
    ctx: click.Context,
    plan_calls: list[tuple[str, Path | None]],
) -> None:
    from gobby.agents import srt_runtime

    config = DaemonConfig()
    config.agent_sandbox.enabled = True
    monkeypatch.setattr(CliRuntime, "read_only_operational_config", lambda _runtime: config)

    def restage() -> None:
        raise srt_runtime.SrtRuntimeError("bundled SRT runner checksum mismatch")

    def verify() -> None:
        pytest.fail("failed restage must prevent verification and stop")

    monkeypatch.setattr(install_setup_srt, "restage_srt_runner", restage)
    monkeypatch.setattr(srt_runtime, "verify_srt_installation", verify)

    assert daemon_preflight.restart_start_refusal(ctx) == (
        "Managed SRT sandbox preflight failed: bundled SRT runner checksum mismatch"
    )
    assert plan_calls == []


def test_disabled_sandboxes_skip_srt_verification(
    monkeypatch: pytest.MonkeyPatch,
    ctx: click.Context,
    plan_calls: list[tuple[str, Path | None]],
) -> None:
    from gobby.agents import srt_runtime

    def unexpected_verification() -> None:
        pytest.fail("disabled sandboxes must not require an SRT installation")

    monkeypatch.setattr(srt_runtime, "verify_srt_installation", unexpected_verification)
    monkeypatch.setattr(install_setup_srt, "restage_srt_runner", unexpected_verification)

    assert daemon_preflight.restart_start_refusal(ctx) is None
    assert plan_calls == [(_BOOTSTRAP_URL, None)]


def _raising_plan(message: str) -> Callable[..., str]:
    def plan_schema(_database_url: str, *, gdaemon: Path | None = None) -> str:
        raise SchemaContractError(message)

    return plan_schema


def test_worktree_refusal_is_patchable_at_module_level(
    ctx: click.Context, plan_calls: list[tuple[str, Path | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retargeted patch sites resolve only if the name is a module attribute."""
    monkeypatch.setattr(daemon_preflight, "worktree_daemon_refusal", lambda: "no daemon from /wt")

    assert daemon_preflight.restart_start_refusal(ctx) == "no daemon from /wt"
    assert plan_calls == []


def test_installed_set_refusal_short_circuits_before_the_plan(
    ctx: click.Context, plan_calls: list[tuple[str, Path | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schema_divergence, "binary_set_apply_refusal", lambda *_a, **_k: "mixed installed set"
    )

    assert daemon_preflight.restart_start_refusal(ctx) == "mixed installed set"
    assert plan_calls == []


def test_schema_divergence_refusal_short_circuits_before_the_plan(
    ctx: click.Context, plan_calls: list[tuple[str, Path | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schema_divergence, "schema_apply_refusal", lambda _database: "live hub schema v9 is newer"
    )

    assert daemon_preflight.restart_start_refusal(ctx) == "live hub schema v9 is newer"
    assert plan_calls == []


def test_dirty_bundled_content_refusal_short_circuits_before_the_plan(
    ctx: click.Context, plan_calls: list[tuple[str, Path | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    checked: list[tuple[Path, object | None]] = []
    install_dir = Path("/checkout/src/gobby/install")

    def dirty_refusal(path: Path, *, database: object | None = None) -> str:
        checked.append((path, database))
        return "dirty bundled content"

    monkeypatch.setattr(daemon_preflight, "get_install_dir", lambda: install_dir)
    monkeypatch.setattr(daemon_preflight, "dirty_bundled_content_refusal", dirty_refusal)

    assert daemon_preflight.restart_start_refusal(ctx) == "dirty bundled content"
    assert checked == [(install_dir, None)]
    assert plan_calls == []


def test_clean_bundled_content_preserves_existing_preflight(
    ctx: click.Context, plan_calls: list[tuple[str, Path | None]]
) -> None:
    assert daemon_preflight.restart_start_refusal(ctx) is None
    assert plan_calls == [(_BOOTSTRAP_URL, None)]


def test_plan_failure_becomes_a_refusal_carrying_gdaemon_stderr(
    ctx: click.Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        daemon_preflight,
        "plan_schema",
        _raising_plan(
            "gdaemon schema plan failed: unsupported PostgreSQL schema state: "
            "unrecognized schema lineage; recreate from a verified backup. "
            "Fix the schema inputs or the hub before restarting"
        ),
    )

    refusal = daemon_preflight.restart_start_refusal(ctx)

    assert refusal is not None
    assert "unrecognized schema lineage" in refusal


@pytest.mark.parametrize(
    "detail",
    [
        "gdaemon schema plan timed out after 300 seconds",
        "Failed to launch gdaemon: [Errno 2] No such file or directory",
    ],
)
def test_plan_timeout_and_launch_failures_become_refusals(
    ctx: click.Context, monkeypatch: pytest.MonkeyPatch, detail: str
) -> None:
    monkeypatch.setattr(daemon_preflight, "plan_schema", _raising_plan(detail))

    assert daemon_preflight.restart_start_refusal(ctx) == detail


def test_no_readable_hub_url_skips_the_plan(
    ctx: click.Context, plan_calls: list[tuple[str, Path | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable is not a refusal; the start is simply left unproven."""
    monkeypatch.setattr(
        daemon_preflight,
        "load_bootstrap",
        lambda: SimpleNamespace(datastore_mode="local", database_url=None),
    )

    assert daemon_preflight.restart_start_refusal(ctx) is None
    assert plan_calls == []


def test_installed_mode_plans_with_the_open_hub_conninfo(
    ctx: click.Context, plan_calls: list[tuple[str, Path | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        daemon_preflight,
        "_open_hub",
        lambda _ctx: SimpleNamespace(conninfo="postgresql://opened.example/gobby"),
    )

    assert daemon_preflight.restart_start_refusal(ctx) is None
    assert plan_calls == [("postgresql://opened.example/gobby", None)]


def test_candidate_mode_plans_with_bootstrap_url(
    ctx: click.Context, plan_calls: list[tuple[str, Path | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Candidate mode never opens the hub and never probes the installed set."""
    opened: list[object] = []
    monkeypatch.setattr(daemon_preflight, "_open_hub", lambda _ctx: opened.append(_ctx))
    monkeypatch.setattr(
        schema_divergence,
        "binary_set_apply_refusal",
        lambda *_a, **_k: pytest.fail("the installed set must not be probed in candidate mode"),
    )
    monkeypatch.setattr(
        daemon_preflight,
        "probe_set_member_identity",
        lambda _binary, _member: dict(expected_schema_identity()),
    )

    assert daemon_preflight.restart_start_refusal(ctx, _CANDIDATE) is None
    assert plan_calls == [(_BOOTSTRAP_URL, _CANDIDATE)]
    assert opened == []


def test_candidate_test_cluster_failure_refuses_cutover(
    ctx: click.Context, plan_calls: list[tuple[str, Path | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        daemon_preflight,
        "probe_set_member_identity",
        lambda _binary, _member: dict(expected_schema_identity()),
    )
    calls: list[str] = []

    def verify_cluster(database_url: str) -> None:
        calls.append(database_url)
        raise SchemaContractError("isolated test hub unavailable")

    monkeypatch.setattr(daemon_preflight, "verify_catalog_test_cluster", verify_cluster)
    refusal = daemon_preflight.restart_start_refusal(ctx, _CANDIDATE)
    assert refusal == "isolated test hub unavailable"
    assert calls == [_BOOTSTRAP_URL]
    assert plan_calls == [(_BOOTSTRAP_URL, _CANDIDATE)]


def test_candidate_without_hub_url_refuses_cutover(
    ctx: click.Context, plan_calls: list[tuple[str, Path | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        daemon_preflight,
        "probe_set_member_identity",
        lambda _binary, _member: dict(expected_schema_identity()),
    )
    monkeypatch.setattr(daemon_preflight, "_hub_url", lambda _database: None)
    refusal = daemon_preflight.restart_start_refusal(ctx, _CANDIDATE)
    assert refusal is not None
    assert "requires a readable hub URL" in refusal
    assert plan_calls == []


def test_candidate_identity_mismatch_is_a_refusal(
    ctx: click.Context, plan_calls: list[tuple[str, Path | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = dict(expected_schema_identity())
    stale = {
        **pinned,
        "latest_version": int(pinned["latest_version"]) - 1,
        "latest_checksum": "f" * 64,
    }
    monkeypatch.setattr(
        daemon_preflight, "probe_set_member_identity", lambda _binary, _member: stale
    )

    refusal = daemon_preflight.restart_start_refusal(ctx, _CANDIDATE)

    assert refusal is not None
    assert f"v{stale['latest_version']}" in refusal
    assert f"v{pinned['latest_version']}" in refusal
    assert plan_calls == []


def test_unreadable_candidate_binary_is_a_refusal(
    ctx: click.Context, plan_calls: list[tuple[str, Path | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def probe(_binary: Path, _member: str) -> dict[str, int | str]:
        raise BinarySetCoherenceError("gdaemon schema identity probe failed: exit 1")

    monkeypatch.setattr(daemon_preflight, "probe_set_member_identity", probe)

    refusal = daemon_preflight.restart_start_refusal(ctx, _CANDIDATE)

    assert refusal is not None
    assert "candidate gdaemon cannot be verified" in refusal
    assert plan_calls == []


def test_unreadable_checkout_pin_is_a_refusal(
    ctx: click.Context, plan_calls: list[tuple[str, Path | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        daemon_preflight,
        "probe_set_member_identity",
        lambda _binary, _member: dict(expected_schema_identity()),
    )

    def unreadable_pin() -> dict[str, int | str]:
        raise SchemaContractError("Packaged schema_expected_identity.json is invalid")

    monkeypatch.setattr(daemon_preflight, "expected_schema_identity", unreadable_pin)

    refusal = daemon_preflight.restart_start_refusal(ctx, _CANDIDATE)

    assert refusal is not None
    assert "candidate gdaemon cannot be verified" in refusal
    assert plan_calls == []
