"""Persisted installer-state snapshots and keep/change decisions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import click
import pytest

from gobby.cli._install_state import (
    InstallSectionState,
    prepare_install_state,
    should_configure_section,
    snapshot_install_state,
)

pytestmark = pytest.mark.unit


class _ConfigStore:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def read_snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(overrides=self.values)


class _SecretStore:
    def __init__(self, names: set[str]) -> None:
        self.names = names

    def exists(self, name: str) -> bool:
        return name in self.names

    def get(self, name: str) -> str | None:
        return f"secret-{name}" if name in self.names else None


def _configured_values() -> dict[str, Any]:
    return {
        "ai.embeddings.model": "nomic-embed-text",
        "ai.embeddings.api_base": "http://localhost:1234/v1",
        "ai.embeddings.dim": 768,
        "ai.embeddings.api_key": "$secret:embedding-key",
        "voice.enabled": False,
        "databases.qdrant.url": "http://localhost:6333",
        "databases.qdrant.port": 6333,
        "databases.falkordb.host": "127.0.0.1",
        "databases.falkordb.port": 16379,
        "databases.falkordb.password": "$secret:falkor-key",
    }


def test_snapshot_reports_complete_sections_and_secret_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.cli._install_state.fingerprint_embedding_server_sync",
        lambda _api_base, _api_key=None: "lmstudio",
    )
    state = snapshot_install_state(
        _ConfigStore(_configured_values()),  # type: ignore[arg-type]
        _SecretStore({"embedding-key", "falkor-key"}),  # type: ignore[arg-type]
    )

    state.validate()
    assert state.embedding.configured is True
    assert state.embedding.provider == "lmstudio"
    assert state.embedding.has_api_key is True
    assert state.voice.configured is True
    assert state.voice.enabled is False
    assert state.qdrant.configured is True
    assert state.falkordb.configured is True


def test_prepare_existing_state_prints_each_section(capsys: pytest.CaptureFixture[str]) -> None:
    prepare_install_state(
        _ConfigStore(_configured_values()),  # type: ignore[arg-type]
        _SecretStore({"embedding-key", "falkor-key"}),  # type: ignore[arg-type]
    )

    output = capsys.readouterr().out
    assert "Current optional-service configuration:" in output
    for label in ("Embedding:", "Voice:", "Qdrant:", "FalkorDB:"):
        assert label in output


def test_snapshot_rejects_plaintext_canonical_secret_value() -> None:
    values = _configured_values()
    values["databases.falkordb.password"] = "plaintext"
    state = snapshot_install_state(
        _ConfigStore(values),  # type: ignore[arg-type]
        _SecretStore({"falkor-key"}),  # type: ignore[arg-type]
    )

    with pytest.raises(click.ClickException, match="canonical SecretStore password"):
        state.validate()


def test_configured_noninteractive_section_is_preserved_without_prompt() -> None:
    section = InstallSectionState(configured=True, summary="configured")
    with patch("click.confirm") as confirm:
        assert should_configure_section(section, label="Qdrant", no_interactive=True) is False
    confirm.assert_not_called()


@pytest.mark.parametrize(("answer", "expected"), [(False, False), (True, True)])
def test_configured_interactive_section_prompts_default_keep(answer: bool, expected: bool) -> None:
    section = InstallSectionState(configured=True, summary="configured")
    with patch("click.confirm", return_value=answer) as confirm:
        assert should_configure_section(section, label="FalkorDB", no_interactive=False) is expected
    confirm.assert_called_once_with("Change FalkorDB?", default=False)


def test_explicit_override_reconfigures_without_prompt() -> None:
    section = InstallSectionState(configured=True, summary="configured")
    with patch("click.confirm") as confirm:
        assert (
            should_configure_section(
                section,
                label="embedding provider/model/endpoint",
                no_interactive=False,
                explicit=True,
            )
            is True
        )
    confirm.assert_not_called()


def test_missing_noninteractive_section_runs_setup_without_prompt() -> None:
    section = InstallSectionState(configured=False, summary="not configured")
    with patch("click.confirm") as confirm:
        assert should_configure_section(section, label="Qdrant", no_interactive=True) is True
    confirm.assert_not_called()
