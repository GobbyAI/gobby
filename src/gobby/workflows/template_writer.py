"""Write workflow definitions to YAML template files.

Handles writing rules, pipelines, agents, and variables to disk in the
canonical YAML format expected by the sync functions.
"""

import logging
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "write_rule_template",
    "write_pipeline_template",
    "write_agent_template",
    "write_variable_template",
    "delete_template_file",
    "read_template",
]

logger = logging.getLogger(__name__)


def write_rule_template(
    name: str,
    definition: dict[str, Any],
    output_dir: Path,
    *,
    group: str | None = None,
    tags: list[str] | None = None,
) -> Path:
    """Write a rule definition to a YAML file.

    Uses the multi-rule format with a top-level ``rules:`` key,
    matching the format expected by ``sync_bundled_rules``.

    Args:
        name: Rule name (used as filename)
        definition: Rule definition dict (event, effect, etc.)
        output_dir: Directory to write to
        group: Optional rule group
        tags: Optional tags list

    Returns:
        Path to the written file
    """
    data: dict[str, Any] = {}
    if group:
        data["group"] = group
    if tags:
        data["tags"] = tags
    data["rules"] = {name: definition}
    return _write_yaml(name, data, output_dir)


def write_pipeline_template(
    name: str,
    definition: dict[str, Any],
    output_dir: Path,
) -> Path:
    """Write a pipeline definition to a YAML file.

    Pipelines are written as top-level documents (not nested under a key).

    Args:
        name: Pipeline name (used as filename)
        definition: Full pipeline definition dict
        output_dir: Directory to write to

    Returns:
        Path to the written file
    """
    return _write_yaml(name, definition, output_dir)


def write_agent_template(
    name: str,
    definition: dict[str, Any],
    output_dir: Path,
) -> Path:
    """Write an agent definition to a YAML file.

    Agents are written as top-level documents (not nested under a key).

    Args:
        name: Agent name (used as filename)
        definition: Full agent definition dict
        output_dir: Directory to write to

    Returns:
        Path to the written file
    """
    return _write_yaml(name, definition, output_dir)


def write_variable_template(
    name: str,
    definition: dict[str, Any],
    output_dir: Path,
) -> Path:
    """Write a variable definition to a YAML file.

    Uses the multi-variable format with a top-level ``variables:`` key,
    matching the format expected by ``sync_bundled_variables``.

    Args:
        name: Variable name (used as filename)
        definition: Variable definition dict (type, default, etc.)
        output_dir: Directory to write to

    Returns:
        Path to the written file
    """
    data: dict[str, Any] = {"variables": {name: definition}}
    return _write_yaml(name, data, output_dir)


def delete_template_file(name: str, directory: Path) -> bool:
    """Delete a template YAML file.

    Args:
        name: Template name (filename stem)
        directory: Directory containing the file

    Returns:
        True if file was deleted, False if it didn't exist
    """
    path = directory / f"{name}.yaml"
    if path.exists():
        path.unlink()
        logger.info("Deleted template file", extra={"path": str(path)})
        return True
    return False


def read_template(path: Path) -> dict[str, Any]:
    """Read and parse a YAML template file.

    Args:
        path: Path to YAML file

    Returns:
        Parsed YAML data
    """
    result = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"Invalid template {path}: expected YAML object")
    return result


def _write_yaml(name: str, data: dict[str, Any], output_dir: Path) -> Path:
    """Write data to a YAML file, creating directories as needed."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.yaml"
    path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    logger.info("Wrote template file", extra={"path": str(path)})
    return path
