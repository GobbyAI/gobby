"""Template hash cache for drift detection.

Computes and caches hashes of all bundled template YAML files at startup.
Used to detect when an installed definition in the DB has drifted from
its on-disk template — either because the user modified the installed
copy or because a new Gobby release updated the template.

Each definition type (rules, pipelines, variables, agents) has its own
serialization pipeline, and the hash cache replicates each exactly so
that hash comparisons are valid.
"""

import json
import logging
from pathlib import Path
from typing import Any

import yaml

from gobby.storage.workflow_definitions import (
    WorkflowDefinitionRow,
    compute_definition_hash,
)

logger = logging.getLogger(__name__)


class TemplateHashCache:
    """Cache of hashes for bundled template YAML files.

    Reads template directories once at startup and stores
    name -> hash(definition_json) mappings for cheap drift checks.
    """

    def __init__(self) -> None:
        self._hashes: dict[str, str] = {}
        self._json_cache: dict[str, str] = {}  # name → definition_json (for restore)

    def load(self) -> None:
        """Read all bundled template YAML files and compute their hashes."""
        from gobby.agents.sync import get_bundled_agents_path
        from gobby.skills.sync import get_bundled_skills_path
        from gobby.workflows.sync_pipelines import get_bundled_pipelines_path
        from gobby.workflows.sync_rules import get_bundled_rules_paths
        from gobby.workflows.sync_variables import get_bundled_variables_path

        for rules_path in get_bundled_rules_paths():
            self._load_rules(rules_path)
        self._load_pipelines(get_bundled_pipelines_path())
        self._load_variables(get_bundled_variables_path())
        self._load_agents(get_bundled_agents_path())
        self._load_skills(get_bundled_skills_path())

        logger.info(f"Template hash cache loaded: {len(self._hashes)} definitions")

    # ── Per-type loaders (replicate sync serialization) ──

    def _load_rules(self, rules_dir: Path) -> None:
        """Load rule templates, replicating sync_rules.py serialization."""
        if not rules_dir.exists():
            return

        from gobby.workflows.sync_rules import resolve_sync_placeholders

        for yaml_path in sorted(rules_dir.glob("**/*.yaml")):
            if "deprecated" in yaml_path.relative_to(rules_dir).parts:
                continue
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue

                rules_dict = data.get("rules")
                if not isinstance(rules_dict, dict):
                    continue

                rel_parts = yaml_path.relative_to(rules_dir).parts
                dir_group = rel_parts[0] if len(rel_parts) > 1 else None
                file_group = data.get("group") or dir_group
                file_audience = data.get("audience")
                for rule_name, rule_data in rules_dict.items():
                    if not isinstance(rule_data, dict):
                        continue
                    body_dict = _build_rule_body(rule_data, file_group, file_audience)
                    definition_json = resolve_sync_placeholders(
                        json.dumps(body_dict, sort_keys=True)
                    )
                    self._hashes[rule_name] = compute_definition_hash(definition_json)
                    self._json_cache[rule_name] = definition_json
            except Exception as e:
                logger.warning(f"Failed to hash rule template {yaml_path}: {e}")

    def _load_pipelines(self, pipelines_dir: Path) -> None:
        """Load pipeline templates, replicating sync_pipelines.py serialization."""
        if not pipelines_dir.exists():
            return
        for yaml_path in sorted(pipelines_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                name = data.get("name", yaml_path.stem)
                definition_json = json.dumps(data, sort_keys=True)
                self._hashes[name] = compute_definition_hash(definition_json)
                self._json_cache[name] = definition_json
            except Exception as e:
                logger.warning(f"Failed to hash pipeline template {yaml_path}: {e}")

    def _load_variables(self, variables_dir: Path) -> None:
        """Load variable templates, replicating sync_variables.py serialization."""
        if not variables_dir.exists():
            return
        for yaml_path in sorted(variables_dir.glob("**/*.yaml")):
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue

                variables_dict = data.get("variables")
                if not isinstance(variables_dict, dict):
                    continue

                for var_name, var_data in variables_dict.items():
                    if not isinstance(var_data, dict):
                        continue
                    body_dict: dict[str, Any] = {
                        "variable": var_name,
                        "value": var_data.get("value"),
                    }
                    if var_data.get("description"):
                        body_dict["description"] = var_data["description"]
                    definition_json = json.dumps(body_dict, sort_keys=True)
                    self._hashes[var_name] = compute_definition_hash(definition_json)
                    self._json_cache[var_name] = definition_json
            except Exception as e:
                logger.warning(f"Failed to hash variable template {yaml_path}: {e}")

    def _load_agents(self, agents_dir: Path) -> None:
        """Load agent templates, replicating agents/sync.py serialization."""
        if not agents_dir.exists():
            return

        from gobby.workflows.definitions import AgentDefinitionBody

        for yaml_path in sorted(agents_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                name = data.get("name", yaml_path.stem)
                data["name"] = name
                body = AgentDefinitionBody.model_validate(data)
                body_json = body.model_dump_json()
                self._hashes[name] = compute_definition_hash(body_json)
                self._json_cache[name] = body_json
            except Exception as e:
                logger.warning(f"Failed to hash agent template {yaml_path}: {e}")

    def _load_skills(self, skills_dir: Path) -> None:
        """Load skill templates, replicating skills/sync.py serialization."""
        if not skills_dir.exists():
            return

        try:
            from gobby.skills.loader import SkillLoader

            loader = SkillLoader(default_source_type="filesystem")
            parsed_skills = loader.load_directory(skills_dir, validate=False)
        except Exception as e:
            logger.warning(f"Failed to load bundled skills for hashing: {e}")
            return

        for parsed in parsed_skills:
            try:
                body: dict[str, Any] = {
                    "name": parsed.name,
                    "description": parsed.description,
                    "content": parsed.content,
                    "version": parsed.version,
                    "license": parsed.license,
                    "compatibility": parsed.compatibility,
                    "allowed_tools": parsed.allowed_tools,
                    "metadata": parsed.metadata,
                    "always_apply": parsed.always_apply,
                    "injection_format": parsed.injection_format,
                }
                definition_json = json.dumps(body, sort_keys=True)
                self._hashes[parsed.name] = compute_definition_hash(definition_json)
                self._json_cache[parsed.name] = definition_json
            except Exception as e:
                logger.warning(f"Failed to hash skill template {parsed.name}: {e}")

    # ── Drift detection ──

    def get_hash(self, name: str) -> str | None:
        """Get the cached hash for a template by name."""
        return self._hashes.get(name)

    def get_template_json(self, name: str) -> str | None:
        """Get the definition_json for a template by name.

        Returns the cached serialized JSON from the template file.
        Returns None if no bundled template exists for this name.
        """
        return self._json_cache.get(name)

    def has_drift(self, row: WorkflowDefinitionRow) -> bool:
        """Check if an installed definition has drifted from its template.

        Returns False if no template exists for this name (user-created definition).
        """
        template_hash = self._hashes.get(row.name)
        if template_hash is None:
            return False
        installed_hash = compute_definition_hash(row.definition_json)
        return template_hash != installed_hash

    def annotate_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add has_template_update field to a list of definition dicts.

        Convenience method for API responses — mutates dicts in place.
        """
        for row_dict in rows:
            name = row_dict.get("name")
            definition_json = row_dict.get("definition_json")
            if name and definition_json:
                template_hash = self._hashes.get(name)
                if template_hash is not None:
                    installed_hash = compute_definition_hash(definition_json)
                    row_dict["has_template_update"] = template_hash != installed_hash
                else:
                    row_dict["has_template_update"] = False
            else:
                row_dict["has_template_update"] = False
        return rows


_instance: TemplateHashCache | None = None


def get_template_hash_cache() -> TemplateHashCache:
    """Get or create the singleton TemplateHashCache.

    Loads template hashes on first access.
    """
    global _instance
    if _instance is None:
        _instance = TemplateHashCache()
        _instance.load()
    return _instance


def _build_rule_body(
    rule_data: dict[str, Any],
    file_group: str | None = None,
    file_audience: str | None = None,
) -> dict[str, Any]:
    """Build a rule body dict from YAML data, matching sync_rules.py logic."""
    body_dict: dict[str, Any] = {
        "event": rule_data.get("event"),
    }
    if "effects" in rule_data:
        body_dict["effects"] = rule_data["effects"]
    elif "effect" in rule_data:
        body_dict["effects"] = [rule_data["effect"]]
    if rule_data.get("when"):
        body_dict["when"] = rule_data["when"]
    if rule_data.get("match"):
        body_dict["match"] = rule_data["match"]
    group = rule_data.get("group", file_group)
    if group:
        body_dict["group"] = group
    if rule_data.get("agent_scope"):
        body_dict["agent_scope"] = rule_data["agent_scope"]
    audience = rule_data.get("audience", file_audience)
    if audience:
        body_dict["audience"] = audience
    if rule_data.get("tools"):
        body_dict["tools"] = rule_data["tools"]
    return body_dict
