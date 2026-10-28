"""Keep first-run acknowledgements aligned with the repository's launch defaults."""

import json
import shutil
import tomllib
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

import psycopg
import pytest
import yaml
from setuptools import Distribution

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


def _packaged_defaults_path() -> Path:
    return Path(str(files("gobby").joinpath("data/codex_launch_defaults.json")))


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
    cold_reads = parse.call_count
    second = packaged_codex_launch_models()
    assert first == second
    assert cold_reads > 0
    assert parse.call_count == cold_reads


def test_packaged_launch_defaults_match_project_policy() -> None:
    root = Path(__file__).resolve().parents[2]
    document = yaml.safe_load(
        (root / ".gobby/workflows/rules/project-operating-policy.yaml").read_text()
    )
    policy = next(
        effect["value"]
        for effect in document["rules"]["project-operating-policy"]["effects"]
        if effect.get("variable") == "project_policy"
    )
    path = _packaged_defaults_path()
    assert path.is_file(), "Launch defaults must ship independently of the source checkout"
    assert json.loads(path.read_text()) == {
        key: policy[key] for key in ("launch_defaults", "role_launch_defaults")
    }


def test_launch_defaults_are_in_built_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build a small wheel with the repository's actual package-data configuration."""
    root = Path(__file__).resolve().parents[2]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    source = _packaged_defaults_path()
    assert source.is_file(), "Launch defaults package data is missing"
    package = tmp_path / "src/gobby"
    (package / "data").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    shutil.copyfile(source, package / "data" / source.name)
    monkeypatch.chdir(tmp_path)
    distribution = Distribution(
        {
            "name": "gobby-data-check",
            "version": "0.0.0",
            "script_name": "setup.py",
            "packages": ["gobby"],
            "package_dir": {"": "src"},
            "package_data": config["tool"]["setuptools"]["package-data"],
        }
    )
    distribution.run_command("bdist_wheel")
    wheels = list((tmp_path / "dist").glob("*.whl"))
    assert len(wheels) == 1
    with ZipFile(wheels[0]) as wheel:
        assert wheel.read("gobby/data/codex_launch_defaults.json") == source.read_bytes()


def _codex_models(defaults: dict[str, Any]) -> set[str]:
    models: set[str] = set()
    for key, value in defaults.items():
        if isinstance(value, dict):
            models.update(_codex_models(value))
        elif (key == "model" or key.endswith("_model")) and isinstance(value, str):
            value = value.strip()
            if not value or value == "inherit":
                continue
            provider_key = key.removesuffix("model") + "provider"
            provider = defaults.get(provider_key)
            if provider == "codex" or (provider in (None, "inherit") and value.startswith("gpt-")):
                models.add(value)
    return models


def _default_codex_launch_models() -> set[str]:
    root = Path(__file__).resolve().parents[2]
    policy_file = root / ".gobby/workflows/rules/project-operating-policy.yaml"
    document = yaml.safe_load(policy_file.read_text())
    effects = document["rules"]["project-operating-policy"]["effects"]
    policy = next(
        effect["value"] for effect in effects if effect.get("variable") == "project_policy"
    )
    models = _codex_models(policy["launch_defaults"])
    models.update(_codex_models(policy["role_launch_defaults"]))
    agents = root / "src/gobby/install/shared/workflows/agents"
    for definition_file in agents.glob("*.yaml"):
        definition = cast(dict[str, Any], yaml.safe_load(definition_file.read_text()))
        models.update(_codex_models(definition))
    return models


@pytest.mark.parametrize("entrypoint", ["workspace", "install"])
def test_fresh_codex_home_acknowledges_every_default_launch_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    """Adding a Codex model to policy or a bundled seat must also seed its NUX."""
    models = _default_codex_launch_models()
    assert "gpt-6.1-terra" in models
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
    assert {model: acknowledgements.get(model) for model in models} == dict.fromkeys(models, 4)


def test_packaged_codex_models_seed_without_checkout_or_policy_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    (installed / "data").mkdir(parents=True)
    shutil.copyfile(_packaged_defaults_path(), installed / "data/codex_launch_defaults.json")
    agents = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows/agents"
    installed_agents = installed / "install/shared/workflows/agents"
    shutil.copytree(agents, installed_agents)
    resource_root = MagicMock(return_value=installed)
    monkeypatch.setattr("gobby.agents.trust.files", resource_root)
    monkeypatch.setattr("gobby.agents.sync.get_bundled_agents_path", lambda: installed_agents)
    monkeypatch.chdir(tmp_path)

    with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
        result = seed_gobby_home_trust("codex", gobby_home=gobby_home)

    assert result["success"] is True
    resource_root.assert_called_once_with("gobby")
    config = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    models = _codex_models(json.loads(_packaged_defaults_path().read_text()))
    for path in agents.glob("*.yaml"):
        models.update(_codex_models(yaml.safe_load(path.read_text())))
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
        definition_json={
            "effects": [
                {
                    "variable": "project_policy",
                    "value": {
                        "launch_defaults": {
                            "developer_provider": "codex",
                            "developer_model": "gpt-7-sol",
                        }
                    },
                }
            ]
        }
    )
    monkeypatch.setattr(RuleDefinitionManager, "get_by_name", lambda *_args, **_kwargs: policy_row)
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

    monkeypatch.setattr(RuleDefinitionManager, "get_by_name", unreadable)

    assert active_codex_launch_models(cast(Any, object()), "project-1") == ()
