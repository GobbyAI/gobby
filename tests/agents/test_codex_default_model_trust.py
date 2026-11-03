"""Keep first-run acknowledgements aligned with the repository's launch defaults."""

import shutil
import tomllib
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import psycopg
import pytest
import yaml

from gobby.agents.trust import (
    active_codex_launch_models,
    codex_model_ids_in_policy,
    packaged_codex_launch_models,
    pre_approve_directory,
    seed_gobby_home_trust,
)
from gobby.storage.definitions.rules import RuleDefinitionManager

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolate_packaged_model_cache() -> Iterator[None]:
    packaged_codex_launch_models.cache_clear()
    yield
    packaged_codex_launch_models.cache_clear()


@pytest.mark.parametrize("model", ["inherit", "", "   "])
def test_codex_launch_defaults_exclude_non_model_values(model: str) -> None:
    assert codex_model_ids_in_policy({"provider": "codex", "model": model}) == set()
    assert (
        codex_model_ids_in_policy({"reviewer_provider": "codex", "reviewer_model": model}) == set()
    )


def test_packaged_models_are_parsed_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    parse = MagicMock(wraps=yaml.safe_load)
    monkeypatch.setattr("gobby.agents.trust.yaml.safe_load", parse)
    first = packaged_codex_launch_models()
    assert first == frozenset({"synthetic-codex-model"})
    cold_reads = parse.call_count
    second = packaged_codex_launch_models()
    assert first == second
    assert cold_reads > 0
    assert parse.call_count == cold_reads


@pytest.fixture(autouse=True)
def synthetic_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "custom.yaml").write_text("provider: codex\nmodel: synthetic-codex-model\n")
    (agents / "other.yaml").write_text("provider: claude\nmodel: synthetic-other-model\n")
    monkeypatch.setattr("gobby.agents.sync.get_bundled_agents_path", lambda: agents)
    return agents


@pytest.mark.parametrize("entrypoint", ["workspace", "install"])
def test_fresh_codex_home_acknowledges_packaged_and_custom_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    synthetic_agents: Path,
) -> None:
    """Both entrypoints combine packaged defaults with arbitrary Codex definitions."""
    models = set()
    models.add("synthetic-codex-model")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
        if entrypoint == "workspace":
            pre_approve_directory("codex", workspace)
        else:
            result = seed_gobby_home_trust("codex", gobby_home=workspace)
            assert result["success"] is True

    config = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    acknowledgements = config["tui"]["model_availability_nux"]
    assert acknowledgements == dict.fromkeys(models, 4)


def test_packaged_codex_models_seed_without_checkout_or_policy_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synthetic_agents: Path
) -> None:
    """A fresh home with no active policy row acknowledges the packaged launch models.

    The seed reads nothing from a checkout, so this holds where no ``.gobby`` policy sits
    beside the installed package.
    """
    monkeypatch.delenv("CODEX_HOME", raising=False)
    gobby_home = tmp_path / "gobby"
    gobby_home.mkdir()
    # Reproduce installed package resources without any adjacent checkout policy.
    installed = tmp_path / "site-packages/gobby"
    installed.mkdir(parents=True)
    installed_agents = installed / "definitions"
    shutil.copytree(synthetic_agents, installed_agents)
    monkeypatch.setattr("gobby.agents.sync.get_bundled_agents_path", lambda: installed_agents)
    monkeypatch.chdir(tmp_path)

    with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
        result = seed_gobby_home_trust("codex", gobby_home=gobby_home)

    assert result["success"] is True
    config = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    models = set()
    models.add("synthetic-codex-model")
    assert config["tui"]["model_availability_nux"] == dict.fromkeys(models, 4)


def test_new_bundled_agent_model_is_seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agents = tmp_path / "bundled_agents"
    agents.mkdir()
    (agents / "future.yaml").write_text("provider: codex\nmodel: gpt-future-seat\n")
    monkeypatch.setattr("gobby.agents.sync.get_bundled_agents_path", lambda: agents)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
        result = seed_gobby_home_trust("codex", gobby_home=tmp_path / "gobby")
    assert result["success"] is True
    config = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    assert config["tui"]["model_availability_nux"]["gpt-future-seat"] == 4


def test_active_policy_codex_model_is_seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Codex model added to the DB-active project policy is acknowledged at spawn."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    policy_row = SimpleNamespace(
        enabled=True,
        source="installed",
        definition_json={
            "effects": [
                {
                    "type": "set_variable",
                    "variable": "project_policy",
                    "value": {
                        "launch_defaults": {
                            "developer_provider": "codex",
                            "developer_model": "gpt-7-sol",
                        }
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(
        RuleDefinitionManager, "list_effective", lambda *_args, **_kwargs: [policy_row]
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    models = active_codex_launch_models(cast(Any, object()), "project-1")
    with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
        pre_approve_directory("codex", workspace, models)

    assert models == ("gpt-7-sol",)
    config = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    assert config["tui"]["model_availability_nux"]["gpt-7-sol"] == 4

    # Caching package resources must not freeze the DB-active defaults.
    policy_row.definition_json["effects"][0]["value"]["launch_defaults"]["developer_model"] = (
        "gpt-7-terra"
    )
    updated_models = active_codex_launch_models(cast(Any, object()), "project-1")
    assert updated_models == ("gpt-7-terra",)
    with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
        pre_approve_directory("codex", workspace, updated_models)
    updated_config = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    assert updated_config["tui"]["model_availability_nux"]["gpt-7-terra"] == 4


@pytest.mark.parametrize(
    "failure",
    [psycopg.OperationalError("hub unavailable"), ValueError("created_at is required")],
    ids=["database-error", "malformed-row"],
)
def test_active_codex_models_empty_without_database_or_readable_row(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """No database, a failed read, or a malformed row adds nothing; packaged defaults seed."""
    assert active_codex_launch_models(None, None) == ()

    def unreadable(*_args: Any, **_kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr(RuleDefinitionManager, "list_effective", unreadable)

    assert active_codex_launch_models(cast(Any, object()), "project-1") == ()


@pytest.mark.parametrize(
    ("enabled", "source"), [(False, "installed"), (True, "custom"), (True, "project")]
)
def test_active_models_ignore_disabled_or_untrusted_writers(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, source: str
) -> None:
    row = SimpleNamespace(
        enabled=enabled,
        source=source,
        definition_json={
            "effects": [
                {
                    "type": "set_variable",
                    "variable": "project_policy",
                    "value": {
                        "launch_defaults": {
                            "fixture_provider": "codex",
                            "fixture_model": "untrusted-model",
                        }
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(RuleDefinitionManager, "list_effective", lambda *_args, **_kwargs: [row])
    assert active_codex_launch_models(cast(Any, object()), "project-1") == ()
