"""Restart preflight refuses a bad runtime agent build root before anything stops."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from gobby.cli import daemon_preflight
from gobby.cli.runtime import CliRuntime
from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit


def _settle_other_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass every check that runs before the build-root check, so only that check decides."""
    monkeypatch.setattr(daemon_preflight, "load_bootstrap", lambda: None)
    monkeypatch.setattr(daemon_preflight, "node_daemon_refusal", lambda _bootstrap: None)
    monkeypatch.setattr(daemon_preflight, "worktree_daemon_refusal", lambda: None)
    monkeypatch.setattr(daemon_preflight, "open_preflight_hub", lambda _runtime: None)
    monkeypatch.setattr(daemon_preflight, "_hub_url", lambda _database: None)
    monkeypatch.setattr(daemon_preflight, "dirty_bundled_content_refusal", lambda *_a, **_k: None)
    monkeypatch.setattr("gobby.storage.schema_divergence.binary_set_apply_refusal", lambda: None)
    monkeypatch.setattr(
        "gobby.storage.schema_divergence.schema_apply_refusal", lambda *_a, **_k: None
    )


def _runtime_with_build_root(value: str) -> CliRuntime:
    """Stand in for the CLI runtime; the preflight only reads its operational config."""
    config = DaemonConfig()
    config.agent_build.build_root = value
    return cast(CliRuntime, SimpleNamespace(read_only_operational_config=lambda: config))


def test_restart_refuses_a_bad_runtime_build_root(monkeypatch: pytest.MonkeyPatch) -> None:
    _settle_other_checks(monkeypatch)

    refusal = daemon_preflight.restart_start_refusal(
        _runtime_with_build_root("relative/build-root")
    )

    assert refusal is not None
    assert "agent_build.build_root is invalid" in refusal
    assert "must be absolute" in refusal


def test_restart_does_not_refuse_on_an_empty_build_root(monkeypatch: pytest.MonkeyPatch) -> None:
    _settle_other_checks(monkeypatch)

    refusal = daemon_preflight.restart_start_refusal(_runtime_with_build_root(""))

    assert refusal is None or "agent_build.build_root" not in refusal
