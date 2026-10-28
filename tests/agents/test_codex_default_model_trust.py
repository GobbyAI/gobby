"""Keep first-run acknowledgements aligned with the repository's launch defaults."""

import tomllib
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
import yaml

from gobby.agents.trust import pre_approve_directory, seed_gobby_home_trust

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
