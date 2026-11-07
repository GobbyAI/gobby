"""Strict YAML loading for definition files.

PyYAML keeps the last of two identical mapping keys without complaint, so a
duplicated line silently replaces the value above it. Definition files must
fail instead. The failure is a ``yaml.YAMLError``, which every sync path already
records against the one file that declared the duplicate.
"""

from collections.abc import Callable
from typing import Any, cast

import yaml

__all__ = ["StrictYamlLoader", "load_strict_yaml"]


class StrictYamlLoader(yaml.SafeLoader):
    """Reject duplicate declarations while retaining YAML merge precedence."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        declared: set[Any] = set()
        construct_object = cast(Callable[[yaml.Node, bool], Any], self.construct_object)
        for key_node, _ in node.value:
            key = (
                "<<"
                if key_node.tag == "tag:yaml.org,2002:merge"
                else construct_object(key_node, deep)
            )
            try:
                duplicate = key in declared
                declared.add(key)
            except TypeError as error:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable key",
                    key_node.start_mark,
                ) from error
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate mapping key {key!r}",
                    key_node.start_mark,
                )
        return super().construct_mapping(node, deep=deep)


def load_strict_yaml(text: str) -> Any:
    """Parse one YAML document, refusing duplicate mapping keys at any depth."""
    # StrictYamlLoader subclasses yaml.SafeLoader and only adds duplicate-key refusal.
    return yaml.load(text, Loader=StrictYamlLoader)  # nosec B506 # safe: SafeLoader subclass
