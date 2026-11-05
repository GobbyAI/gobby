"""A process whose spawn markers are cleared passes only when it can read the operator credential."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gobby.releases import activation
from gobby.releases.activation import agent_activation_refusal

pytestmark = pytest.mark.unit

_MARKERS = ("GOBBY_AGENT_RUN_ID", "GOBBY_MANAGED_EXECUTION_ID", "GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
_OPERATIONS = ["start", "stop", "restart", "cutover"]


@pytest.fixture
def cleared_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _MARKERS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("operation", _OPERATIONS)
def test_cleared_markers_without_operator_credential_are_refused(
    monkeypatch: pytest.MonkeyPatch, cleared_markers: None, operation: str
) -> None:
    monkeypatch.setattr(activation, "operator_credential_readable", lambda: False)

    refusal = agent_activation_refusal(operation)

    assert refusal is not None
    assert refusal.startswith("spawned_agent_refused")


@pytest.mark.parametrize("operation", _OPERATIONS)
def test_cleared_markers_with_operator_credential_pass(
    monkeypatch: pytest.MonkeyPatch, cleared_markers: None, operation: str
) -> None:
    monkeypatch.setattr(activation, "operator_credential_readable", lambda: True)

    assert agent_activation_refusal(operation) is None


def test_missing_bootstrap_is_not_an_operator_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(activation, "daemon_bootstrap_path", lambda: tmp_path / "absent.yaml")

    assert activation.operator_credential_readable() is False


def test_bootstrap_behind_a_denied_directory_is_not_an_operator_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads through a denied directory")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "bootstrap.yaml").write_text("database_url: postgresql://operator@127.0.0.1:1/hub\n")
    locked.chmod(0o000)
    monkeypatch.setattr(activation, "daemon_bootstrap_path", lambda: locked / "bootstrap.yaml")

    try:
        assert activation.operator_credential_readable() is False
    finally:
        locked.chmod(0o700)


def test_unparseable_bootstrap_is_still_the_operator_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A malformed bootstrap still belongs to the operator; the start path reports its error."""
    bootstrap = tmp_path / "bootstrap.yaml"
    bootstrap.write_text("database_url: [unclosed\n")
    monkeypatch.setattr(activation, "daemon_bootstrap_path", lambda: bootstrap)

    assert activation.operator_credential_readable() is True


def test_readable_bootstrap_is_the_operator_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bootstrap = tmp_path / "bootstrap.yaml"
    bootstrap.write_text("database_url: postgresql://operator@127.0.0.1:1/hub\n")
    monkeypatch.setattr(activation, "daemon_bootstrap_path", lambda: bootstrap)

    assert activation.operator_credential_readable() is True
