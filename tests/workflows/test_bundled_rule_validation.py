"""Every bundled definition follows the same contract as an arbitrary user rule."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.rule_validation_yaml import validate_rule_yaml
from gobby.workflows.sync_rules import _iter_active_rule_files, get_bundled_rules_paths

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("filename", ["capability_signing_key", ".capability_signing_key-staged"])
def test_credential_path_blocks_cover_the_daemon_signing_authority(filename: str) -> None:
    matched = 0
    for _, path in _iter_active_rule_files(get_bundled_rules_paths()):
        definition: dict[str, Any] = yaml.safe_load(path.read_text())
        for rule in definition.get("rules", {}).values():
            for effect in rule.get("effects", []):
                pattern = effect.get("command_pattern", "")
                if (
                    pattern
                    and effect.get("type") == "block"
                    and re.search(pattern, "cat ~/.gobby/bootstrap.yaml")
                ):
                    matched += 1
                    assert re.search(pattern, f"cat ~/.gobby/{filename}"), path
    assert matched > 0, "No bundled credential-path protection was exercised"


@pytest.mark.filterwarnings("error::SyntaxWarning")
@pytest.mark.parametrize(
    "yaml_file",
    [path for _, path in _iter_active_rule_files(get_bundled_rules_paths())],
    ids=lambda path: path.name,
)
def test_bundled_rule_yaml_and_engine_dry_run(yaml_file: Path) -> None:
    reports = validate_rule_yaml(yaml_file.read_text(), source=str(yaml_file))
    assert reports, f"No rules discovered in {yaml_file}"
    for report in reports:
        report.raise_for_errors()


def test_user_rule_uses_identical_yaml_and_engine_validation() -> None:
    reports = validate_rule_yaml("""
version: '1.0.0'
group: custom
rules:
  user-invented:
    event: before_tool
    enabled: false
    when: custom_flag
    effect:
      type: block
      reason: '{{ missing_message }}'
""")
    assert len(reports) == 1
    assert reports[0].valid
    assert reports[0].warnings


@pytest.mark.parametrize("text", ["rules: [", "[]", "rules: []", "rules: {}"])
def test_invalid_yaml_structure_is_rejected(text: str) -> None:
    reports = validate_rule_yaml(text, source="synthetic.yaml")
    assert len(reports) == 1
    assert not reports[0].valid
    assert reports[0].source == "synthetic.yaml"


def test_one_invalid_rule_does_not_hide_other_entries() -> None:
    reports = validate_rule_yaml("""
rules:
  broken:
    event: before_tool
    effect: {type: block, reason: x}
    when: '('
  valid:
    event: before_tool
    effect: {type: block, reason: x}
""")
    assert [report.valid for report in reports] == [False, True]
