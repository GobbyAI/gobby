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
    """Render a safely quoted call; references load through get_skill names."""
    if ":" not in value:
        return 'call_tool("gobby-skills", "get_skill", {"name":' + json.dumps(value) + "})"
    return instruction_batch_call([parse_instruction_requirement(value).identity])


def instruction_batch_call(values: list[str]) -> str:
    """Render one get_skill call that loads every identity in order."""
    arguments = json.dumps({"names": values}, separators=(",", ":"))
    return f'call_tool("gobby-skills", "get_skill", {arguments})'


def instruction_fetch_directive(value: str) -> str:
    """Keep plain-skill wording stable; references name their completion proof."""
    try:
        call = instruction_fetch_call(value)
    except ValueError as exc:
        return f"Invalid instruction requirement: {exc}. Correct the requirement before continuing."
    if ":" not in value:
        return f"Load and fully read the skill in its own outer tool result: {call}. Then continue."
    return (
        f"Load and fully read the reference in its own outer tool result: {call}. "
        "Follow page.next_cursor with only cursor until null; it is loaded once a "
        "page.completed lists it. Then continue."
    )


def instruction_batch_directive(values: list[str]) -> str:
    """Name every identity in one call; invalid identities are reported separately."""
    valid: list[str] = []
    invalid: list[str] = []
    for value in values:
        try:
            valid.append(parse_instruction_requirement(value).identity)
        except ValueError as exc:
            invalid.append(
                f"Invalid instruction requirement: {exc}. Correct the requirement before "
                "continuing."
            )
    lines = invalid
    if valid:
        lines = [
            "Load and fully read these instructions with one call, in its own outer tool "
            f"result: {instruction_batch_call(valid)}. Follow page.next_cursor with only cursor "
            "until null; each is loaded once a page.completed lists it. Then continue.",
            *invalid,
        ]
    return "\n".join(lines)


INSTRUCTION_CALL_PREFIXES = ("get_skill(", "get_skill_file(")


def instruction_call_form(value: str) -> str:
    """Render the bare keyword call: the fewest characters that still name a target exactly."""
    if ":" not in value:
        return f"get_skill(name={json.dumps(value)})"
    identity = parse_instruction_requirement(value).identity
    return f"get_skill(names={json.dumps([identity])})"


def instruction_batch_call_form(values: list[str]) -> str:
    """Render the bare keyword call that loads several identities at once."""
    identities = [parse_instruction_requirement(value).identity for value in values]
    return f"get_skill(names={json.dumps(identities)})"


def is_instruction_call_line(line: str) -> bool:
    """Report whether a reason line is the bare call list a skill-step denial leads with."""
    return line.startswith(INSTRUCTION_CALL_PREFIXES)
