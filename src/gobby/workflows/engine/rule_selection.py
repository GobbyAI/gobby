"""Enabled-rule loading, definition binding, and applicability selection."""

import json
import logging
from typing import Any

from pydantic import ValidationError

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.revisions import get_definitions_revision
from gobby.storage.definitions.rules import RuleDefinitionManager, RuleDefinitionRow
from gobby.workflows.definitions import AgentDefinitionBody, RuleDefinitionBody, RuleTriggerEvent
from gobby.workflows.selectors import (
    agent_scope_matches,
    binds_named_definition,
    rule_matches_agent,
)

logger = logging.getLogger(__name__)


class RuleSelectionMixin:
    rule_manager: RuleDefinitionManager
    agent_manager: AgentDefinitionManager
    _agent_def_cache: dict[tuple[str, str | None], AgentDefinitionBody | None]
    _agent_def_cache_revision: int

    def _load_rules(
        self,
        rule_events: list[RuleTriggerEvent],
        *,
        project_id: str | None = None,
    ) -> list[tuple[RuleDefinitionRow, RuleDefinitionBody]]:
        """Load enabled rules matching any trigger event, sorted by priority.

        Global rows always apply; project-owned rows apply only to events from
        that project.
        """
        ordered: list[tuple[int, RuleDefinitionRow, RuleDefinitionBody]] = []
        seen_rows: set[str] = set()

        for trigger_index, rule_event in enumerate(rule_events):
            rows = self.rule_manager.list_by_event(
                event=rule_event.value,
                project_id=project_id,
                enabled=True,
            )
            for row in rows:
                if row.id in seen_rows:
                    continue
                try:
                    payload = row.definition_json
                    body = (
                        RuleDefinitionBody.model_validate(payload)
                        if isinstance(payload, dict)
                        else RuleDefinitionBody.model_validate_json(payload)
                    )
                    ordered.append((trigger_index, row, body))
                    seen_rows.add(row.id)
                except Exception as e:
                    logger.warning("Failed to parse rule %s: %s", row.name, e)

        ordered.sort(key=lambda item: (item[1].priority, item[0], item[1].name))
        return [(row, body) for _, row, body in ordered]

    def _filter_by_agent_scope(
        self,
        rules: list[tuple[RuleDefinitionRow, RuleDefinitionBody]],
        agent_type: str | None,
    ) -> list[tuple[RuleDefinitionRow, RuleDefinitionBody]]:
        """Filter rules by agent_scope.

        - Rules with no agent_scope (None) are global — always included.
        - Rules with agent_scope require _agent_type to be in the list.
        - If no _agent_type is set, only global rules are included.
        """
        return [
            (row, body) for row, body in rules if agent_scope_matches(body.agent_scope, agent_type)
        ]

    def _filter_by_audience(
        self,
        rules: list[tuple[RuleDefinitionRow, RuleDefinitionBody]],
        variables: dict[str, Any],
    ) -> list[tuple[RuleDefinitionRow, RuleDefinitionBody]]:
        """Filter rules by broad runtime audience."""
        return [(row, body) for row, body in rules if self._audience_matches(body, variables)]

    def _audience_matches(
        self,
        body: RuleDefinitionBody,
        variables: dict[str, Any],
    ) -> bool:
        audience = body.audience
        if audience is None or audience == "all":
            return True

        explicit = variables.get("_audience")
        agent_type = variables.get("_agent_type")
        is_spawned = bool(variables.get("is_spawned_agent"))

        if audience == explicit or audience == agent_type:
            return True
        if audience == "autonomous":
            return is_spawned or agent_type == "autonomous"
        if audience == "interactive":
            return not is_spawned and agent_type in (None, "default", "interactive")
        return False

    def _filter_by_active_rules(
        self,
        rules: list[tuple[RuleDefinitionRow, RuleDefinitionBody]],
        variables: dict[str, Any],
        *,
        project_id: str | None = None,
    ) -> list[tuple[RuleDefinitionRow, RuleDefinitionBody]]:
        """Use enabled definition selectors, or baseline rules when no definition resolves.

        Mandatory rules are active under every definition, including one that selects none.
        """
        agent = self._load_active_agent_definition(
            variables.get("_agent_type"),
            project_id=project_id,
        )
        if agent is not None:
            return [
                (row, body)
                for row, body in rules
                if body.mandatory or rule_matches_agent(agent, row)
            ]

        # The cached names are a projection, not authority after definition removal.
        # Only an unbound session gets the baseline; a named one keeps mandatory rules only.
        baseline = not binds_named_definition(variables.get("_agent_type"))
        return [
            (row, body)
            for row, body in rules
            if body.mandatory or (baseline and "default" in (row.tags or []))
        ]

    def _load_active_agent_definition(
        self,
        agent_type: Any,
        *,
        project_id: str | None = None,
    ) -> AgentDefinitionBody | None:
        if not isinstance(agent_type, str) or not agent_type:
            return None

        revision = get_definitions_revision("agents")
        if revision != self._agent_def_cache_revision:
            self._agent_def_cache.clear()
            self._agent_def_cache_revision = revision

        cache_key = (agent_type, project_id)
        if cache_key in self._agent_def_cache:
            return self._agent_def_cache[cache_key]

        agent: AgentDefinitionBody | None = None
        row = self.agent_manager.get_by_name(agent_type, project_id=project_id)
        if row is not None and row.enabled:
            try:
                data = row.definition_json
                if isinstance(data, str):
                    data = json.loads(data)
                if isinstance(data, dict):
                    data = {"name": row.name, **data}
                agent = AgentDefinitionBody.model_validate(data)
            except (json.JSONDecodeError, TypeError, ValidationError) as exc:
                raise ValueError(f"Invalid active agent definition {agent_type!r}: {exc}") from exc

        self._agent_def_cache[cache_key] = agent
        return agent
