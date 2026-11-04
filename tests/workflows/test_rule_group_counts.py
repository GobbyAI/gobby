"""The rule group table in rules/AGENTS.md matches the bundled rule YAML it describes."""

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

RULES_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "gobby"
    / "install"
    / "shared"
    / "workflows"
    / "rules"
)
GROUP_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|\s*(\d+)\s*\|", re.MULTILINE)


def _documented_counts() -> dict[str, int]:
    text = (RULES_DIR / "AGENTS.md").read_text(encoding="utf-8")
    return {group: int(count) for group, _directory, count in GROUP_ROW.findall(text)}


def _bundled_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for group in sorted(path for path in RULES_DIR.iterdir() if path.is_dir()):
        rules = 0
        for source in sorted(group.rglob("*.yaml")):
            if "deprecated" in source.relative_to(RULES_DIR).parts:
                continue
            document = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
            rules += len(document.get("rules") or {})
        counts[group.name] = rules
    return counts


def test_group_table_lists_every_rule_group() -> None:
    assert set(_documented_counts()) == set(_bundled_counts())


def test_group_counts_match_bundled_rule_yaml() -> None:
    assert _documented_counts() == _bundled_counts()
