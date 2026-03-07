"""Agent-type-aware skill injection.

Resolves which skills to inject and in what format based on agent context
(depth, workflow step, task category, agent type). Backward compatible —
skills without audience_config fall back to legacy always_apply behavior.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from gobby.skills.parser import ParsedSkill, SkillAudienceConfig

logger = logging.getLogger(__name__)


@dataclass
class AgentContext:
    """Snapshot of who is requesting skills.

    Built from session metadata, workflow state, and active task at injection time.
    """

    agent_depth: int = 0
    has_human: bool = True
    workflow_name: str | None = None
    workflow_step: str | None = None
    task_type: str | None = None
    task_category: str | None = None
    agent_type: str = "interactive"  # interactive | autonomous | orchestrator | worker
    source: str | None = None

    @classmethod
    def from_session(
        cls,
        session: Any,
        workflow_state: Any | None = None,
        task: dict[str, Any] | None = None,
    ) -> AgentContext:
        """Build context from session + workflow state + active task.

        Args:
            session: Session object with agent_depth attribute
            workflow_state: Dict with workflow variables (session-scoped)
            task: Active task dict with type/category fields
        """
        depth = getattr(session, "agent_depth", 0) or 0
        source = getattr(session, "source", None)
        if source is not None:
            source = str(source.value) if hasattr(source, "value") else str(source)

        wf_name: str | None = None
        wf_step: str | None = None
        if workflow_state:
            wf_name = getattr(workflow_state, "workflow_name", None)
            wf_step = getattr(workflow_state, "current_step", None)
            # Also check step_name for step workflows
            if not wf_step:
                wf_step = getattr(workflow_state, "step_name", None)

        task_type: str | None = None
        task_category: str | None = None
        if task:
            task_type = task.get("task_type")
            task_category = task.get("category")

        # Derive agent_type from depth + workflow name
        agent_type = _derive_agent_type(depth, wf_name)

        return cls(
            agent_depth=depth,
            has_human=(depth == 0),
            workflow_name=wf_name,
            workflow_step=wf_step,
            task_type=task_type,
            task_category=task_category,
            agent_type=agent_type,
            source=source,
        )


def _derive_agent_type(depth: int, workflow_name: str | None) -> str:
    """Derive agent type from depth and workflow name."""
    if depth == 0:
        return "interactive"
    if workflow_name:
        wf_lower = workflow_name.lower()
        if re.search(r"\bbox\b", wf_lower) or re.search(r"\borchestrat", wf_lower):
            return "orchestrator"
        if re.search(r"\bworker\b", wf_lower):
            return "worker"
    return "autonomous"


@dataclass
class SkillProfile:
    """Agent definition skill override profile.

    Loaded from agent YAML `skill_profile` key. Allows agents to
    include/exclude specific skills and override default format.
    """

    audience: str | None = None
    include_skills: list[str] = field(default_factory=list)
    exclude_skills: list[str] = field(default_factory=list)
    default_format: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillProfile:
        """Parse from agent definition YAML dict."""
        return cls(
            audience=data.get("audience"),
            include_skills=data.get("include_skills", []),
            exclude_skills=data.get("exclude_skills", []),
            default_format=data.get("default_format"),
        )


class SkillInjector:
    """Resolves skill selection and format based on agent context.

    Matching logic:
    1. No audience_config → fall back to is_always_apply() (backward compat)
    2. audience → "all" passes; others must match agent_context.agent_type
    3. depth → None passes; int/list/range compared to agent_context.agent_depth
    4. steps → None passes; list checked against agent_context.workflow_step
    5. task_categories → None passes; list checked against agent_context.task_category

    Format resolution chain (highest to lowest priority):
    1. Agent profile default_format
    2. Skill format_overrides[agent_type]
    3. Skill injection_format (default)
    """

    def select_skills(
        self,
        skills: list[ParsedSkill],
        context: AgentContext,
        profile: SkillProfile | None = None,
    ) -> list[tuple[ParsedSkill, str]]:
        """Select skills and resolve formats for the given agent context.

        Args:
            skills: All available skills
            context: Agent context describing who is requesting
            profile: Optional agent definition skill profile override

        Returns:
            List of (skill, resolved_format) tuples, sorted by priority
        """
        results: list[tuple[ParsedSkill, str, int]] = []

        for skill in skills:
            if not self._should_include(skill, context, profile):
                continue

            fmt = self._resolve_format(skill, context, profile)
            priority = skill.audience_config.priority if skill.audience_config else 50
            results.append((skill, fmt, priority))

        # Sort by priority (lower = earlier), then by name for stability
        results.sort(key=lambda x: (x[2], x[0].name))

        return [(skill, fmt) for skill, fmt, _ in results]

    def _should_include(
        self,
        skill: ParsedSkill,
        context: AgentContext,
        profile: SkillProfile | None = None,
    ) -> bool:
        """Check if a skill should be included for this context."""
        # Agent profile include/exclude takes highest priority
        if profile:
            if profile.exclude_skills and skill.name in profile.exclude_skills:
                return False
            if profile.include_skills:
                # When include list is specified, only those skills pass
                return skill.name in profile.include_skills

        # Skills with audience_config use context-aware matching
        if skill.audience_config:
            return self._matches_audience(skill.audience_config, context)

        # Legacy fallback: no audience_config → use always_apply field
        # Use the parsed field directly (not is_always_apply() which re-derives from metadata)
        return skill.always_apply

    def _matches_audience(self, config: SkillAudienceConfig, context: AgentContext) -> bool:
        """Check if an audience config matches the agent context."""
        # Audience check
        if config.audience != "all":
            if config.audience != context.agent_type:
                return False

        # Depth check
        if config.depth is not None:
            if not self._matches_depth(config.depth, context.agent_depth):
                return False

        # Workflow step check
        if config.steps is not None:
            if context.workflow_step not in config.steps:
                return False

        # Task category check
        if config.task_categories is not None:
            if context.task_category not in config.task_categories:
                return False

        # Source check (web UI vs CLI)
        if config.sources is not None:
            if context.source not in config.sources:
                return False

        return True

    def _matches_depth(self, depth_spec: int | list[int] | str, actual_depth: int) -> bool:
        """Check if actual depth matches a depth specification."""
        if isinstance(depth_spec, int):
            return actual_depth == depth_spec
        if isinstance(depth_spec, list):
            return actual_depth in depth_spec
        if isinstance(depth_spec, str):
            # Range format: "0-2" means 0, 1, 2
            try:
                parts = depth_spec.split("-")
                if len(parts) == 2:
                    low, high = int(parts[0]), int(parts[1])
                    return low <= actual_depth <= high
            except (ValueError, IndexError):
                pass
        logger.warning(f"Invalid depth_spec type {type(depth_spec).__name__}: {depth_spec!r}")
        return False

    def _resolve_format(
        self,
        skill: ParsedSkill,
        context: AgentContext,
        profile: SkillProfile | None = None,
    ) -> str:
        """Resolve injection format for a skill given context.

        Priority: profile.default_format > skill.format_overrides[agent_type] > skill.injection_format
        """
        # 1. Agent profile override (highest priority)
        if profile and profile.default_format:
            return profile.default_format

        # 2. Per-audience format override from skill metadata
        if skill.audience_config and skill.audience_config.format_overrides:
            override = skill.audience_config.format_overrides.get(context.agent_type)
            if override:
                return override

        # 3. Skill's default injection_format
        return skill.injection_format
