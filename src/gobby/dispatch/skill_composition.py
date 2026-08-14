"""Deterministic skill validation shared by dispatch and observability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager

if TYPE_CHECKING:
    from gobby.workflows.definitions import AgentDefinitionBody


@dataclass(frozen=True, slots=True)
class SkillCompositionReport:
    """Result of validating the skills an agent must load before work."""

    required_skills: tuple[str, ...]
    additional_skills: tuple[str, ...]
    checked_skills: tuple[str, ...]
    unknown_skills: tuple[str, ...]
    disabled_skills: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    configuration_errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not (self.configuration_errors or self.unknown_skills or self.disabled_skills)

    @property
    def failure_reason(self) -> str | None:
        if self.valid:
            return None
        details = []
        if self.configuration_errors:
            details.append(f"configuration={','.join(self.configuration_errors)}")
        if self.unknown_skills:
            details.append(f"unknown={','.join(self.unknown_skills)}")
        if self.disabled_skills:
            details.append(f"disabled={','.join(self.disabled_skills)}")
        return f"skill_composition_invalid:{';'.join(details)}"

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "required_skills": list(self.required_skills),
            "additional_skills": list(self.additional_skills),
            "checked_skills": list(self.checked_skills),
            "unknown_skills": list(self.unknown_skills),
            "disabled_skills": list(self.disabled_skills),
            "allowed_tools": list(self.allowed_tools),
            "configuration_errors": list(self.configuration_errors),
            "failure_reason": self.failure_reason,
        }


def _normalize_skill_names(
    value: object, field_name: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if value is None:
        return (), ()
    if not isinstance(value, (list, tuple)):
        return (), (f"{field_name}_must_be_list",)
    if any(not isinstance(item, str) or not item.strip() for item in value):
        return (), (f"{field_name}_must_contain_nonempty_strings",)
    return tuple(dict.fromkeys(value)), ()


def inspect_skill_composition(
    db: HubDatabase,
    *,
    project_id: str,
    agent_body: AgentDefinitionBody | None,
    additional_skills: tuple[str, ...],
) -> SkillCompositionReport:
    """Validate required and task-specific skills and report their tool union."""
    required_skills = None
    if agent_body is not None and agent_body.step_workflow is not None:
        required_skills = agent_body.step_workflow.variables.get("required_skills")
    required, required_errors = _normalize_skill_names(
        required_skills,
        "required_skills",
    )
    additional, additional_errors = _normalize_skill_names(
        additional_skills,
        "additional_skills",
    )
    checked = tuple(dict.fromkeys((*required, *additional)))
    unknown: list[str] = []
    disabled: list[str] = []
    allowed_tools: set[str] = set()
    visible_skills = (
        LocalSkillManager(db).list_skills(project_id=project_id, limit=-1) if checked else []
    )
    skills_by_name = {skill.name: skill for skill in visible_skills}

    for name in checked:
        skill = skills_by_name.get(name)
        if skill is None:
            unknown.append(name)
            continue
        if not skill.enabled:
            disabled.append(name)
        allowed_tools.update(skill.allowed_tools or ())

    return SkillCompositionReport(
        required_skills=required,
        additional_skills=additional,
        checked_skills=checked,
        unknown_skills=tuple(unknown),
        disabled_skills=tuple(disabled),
        allowed_tools=tuple(sorted(allowed_tools)),
        configuration_errors=(*required_errors, *additional_errors),
    )
