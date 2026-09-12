"""Exact identities shared by instruction loading and requirement checks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from gobby.skills.validator import NAME_PATTERN


@dataclass(frozen=True)
class InstructionRequirement:
    skill: str
    path: str | None = None

    @property
    def identity(self) -> str:
        return f"{self.skill}:{self.path}" if self.path is not None else self.skill


def parse_instruction_requirement(value: str) -> InstructionRequirement:
    """Reject ambiguous identities instead of normalizing them into another load."""
    if not isinstance(value, str):
        raise ValueError("Instruction requirement must be a string")
    skill, separator, path = value.partition(":")
    if NAME_PATTERN.fullmatch(skill) is None:
        raise ValueError(f"Invalid skill name in instruction requirement: {value!r}")
    if not separator:
        return InstructionRequirement(skill)
    parts = path.split("/")
    if (
        len(parts) < 2
        or parts[0] != "references"
        or not path.endswith(".md")
        or any(part in {"", ".", ".."} for part in parts)
        or any(character in path for character in "\\:%?#")
        or any(character.isspace() or ord(character) < 32 for character in path)
    ):
        raise ValueError(f"Invalid reference path in instruction requirement: {value!r}")
    return InstructionRequirement(skill, path)


def instruction_is_loaded(value: str, variables: Mapping[str, object]) -> bool:
    """Consult only the ledger corresponding to this exact instruction identity."""
    try:
        requirement = parse_instruction_requirement(value)
    except ValueError:
        return False
    key = "loaded_skill_references" if requirement.path is not None else "loaded_skills"
    loaded = variables.get(key)
    return isinstance(loaded, list) and requirement.identity in loaded


def instruction_fetch_call(value: str) -> str:
    """Render a safely quoted call; schema leasing belongs to the directive."""
    if ":" not in value:
        return 'call_tool("gobby-skills", "get_skill", {"name":' + json.dumps(value) + "})"
    requirement = parse_instruction_requirement(value)
    arguments = json.dumps(
        {"name": requirement.skill, "path": requirement.path}, separators=(",", ":")
    )
    return f'call_tool("gobby-skills", "get_skill_file", {arguments})'


def instruction_fetch_directive(value: str) -> str:
    """Keep plain-skill wording stable and explicitly gate reference retrieval."""
    try:
        call = instruction_fetch_call(value)
    except ValueError as exc:
        return f"Invalid instruction requirement: {exc}. Correct the requirement before continuing."
    if ":" not in value:
        return f"Load and fully read the skill in its own outer tool result: {call}. Then continue."
    return (
        "Load and fully read the reference in its own outer tool result. "
        "If its schema is not leased in this context, first call "
        'get_tool_schema(server_name="gobby-skills", tool_name="get_skill_file") '
        "in a separate outer tool result. Then call "
        f"{call}. Follow page.next_cursor using get_skill_file with only cursor until null; "
        "partial pages, listings and the router do not satisfy this requirement. Then continue."
    )
