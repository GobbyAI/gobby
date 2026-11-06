"""Tests for the strict definition YAML loader."""

import pytest
import yaml

from gobby.utils.strict_yaml import load_strict_yaml

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "text",
    [
        "a: 1\na: 2\n",
        "outer:\n  inner: first\n  inner: second\n",
        "items:\n  - {kind: a, kind: b}\n",
    ],
)
def test_rejects_duplicate_mapping_keys_at_any_depth(text: str) -> None:
    with pytest.raises(yaml.YAMLError, match="duplicate mapping key"):
        load_strict_yaml(text)


def test_keeps_yaml_merge_precedence_over_merged_keys() -> None:
    data = load_strict_yaml(
        "base: &base {agent: Base., tools: [x]}\nchild:\n  <<: *base\n  agent: Child.\n"
    )

    assert data["child"] == {"agent": "Child.", "tools": ["x"]}


def test_rejects_unhashable_mapping_key() -> None:
    with pytest.raises(yaml.YAMLError, match="unhashable"):
        load_strict_yaml("? [a, b]\n: value\n")


def test_plain_mapping_loads_unchanged() -> None:
    assert load_strict_yaml("a: 1\nb: [x, y]\n") == {"a": 1, "b": ["x", "y"]}


@pytest.mark.parametrize(
    "text",
    [
        "x: !!python/object/apply:builtins.dict []\n",
        "x: !!python/name:os.system\n",
    ],
)
def test_refuses_non_safe_yaml_tags(text: str) -> None:
    # The loader is safe only while it stays a yaml.SafeLoader subclass, so non-safe tags
    # must keep failing to construct instead of resolving to Python objects.
    with pytest.raises(yaml.YAMLError, match="could not determine a constructor"):
        load_strict_yaml(text)
