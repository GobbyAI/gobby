"""The shared start preflight that restart and cutover run before acting."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

from gobby.cli import daemon_preflight
from gobby.install.bin_set_coherence import BinarySetCoherenceError
from gobby.storage import schema_divergence
from gobby.storage.schema_contract import SchemaContractError, expected_schema_identity

pytestmark = pytest.mark.unit

_CANDIDATE = Path("/workspace/target/release/gdaemon")
_BOOTSTRAP_URL = "postgresql://bootstrap.example/gobby"


@pytest.fixture
def ctx() -> click.Context:
    return click.Context(click.Command("restart"))


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
    monkeypatch.setattr(schema_divergence, "binary_set_apply_refusal", lambda *_a, **_k: None)
    monkeypatch.setattr(schema_divergence, "schema_apply_refusal", lambda _database: None)
    monkeypatch.setattr(daemon_preflight, "_open_hub", lambda _ctx: None)
    monkeypatch.setattr(
        daemon_preflight,
        "load_bootstrap",
        lambda: SimpleNamespace(database_url=_BOOTSTRAP_URL),
    )


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
        daemon_preflight, "load_bootstrap", lambda: SimpleNamespace(database_url=None)
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
