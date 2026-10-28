"""Keep first-run acknowledgements aligned with the repository's launch defaults."""

import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import psycopg
import pytest
import yaml

from gobby.agents.trust import (
    active_codex_launch_models,
    pre_approve_directory,
    seed_gobby_home_trust,
)
from gobby.storage.definitions.rules import RuleDefinitionManager

pytestmark = pytest.mark.unit


def _codex_models(defaults: dict[str, Any]) -> set[str]:
    models: set[str] = set()
    for key, value in defaults.items():
        if isinstance(value, dict):
            models.update(_codex_models(value))
        elif (key == "model" or key.endswith("_model")) and isinstance(value, str):
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

    with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
        result = seed_gobby_home_trust("codex", gobby_home=gobby_home)

    assert result["success"] is True
    config = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    assert config["tui"]["model_availability_nux"] == {
        "gpt-5.6-sol": 4,
        "gpt-6-luna": 4,
        "gpt-6.1-sol": 4,
        "gpt-6.1-terra": 4,
    }


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
