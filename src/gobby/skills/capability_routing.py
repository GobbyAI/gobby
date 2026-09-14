"""Catalog-backed routing; resolving a route never loads instruction bodies."""

from __future__ import annotations

import json
import re
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from gobby.skills.capability_catalog import Capability, CapabilityCatalog, load_capability_catalog
from gobby.skills.instruction_requirements import instruction_fetch_directive
from gobby.skills.parser import ParsedSkill


@dataclass(frozen=True)
class GobbyRoute:
    kind: Literal["help", "menu", "reference", "skill", "unknown"]
    context: str
    arguments: str = ""
    unknown_name: str | None = None


def gobby_help_prefix(prompt: object) -> str | None:
    """Recognize only bare and explicit-help router requests, never work requests."""
    if not isinstance(prompt, str):
        return None
    match = re.fullmatch(r"([/$])gobby(?:\s+help)?\s*", prompt.strip(), re.IGNORECASE)
    return f"{match[1]}gobby" if match else None


def _menu_description(description: str, limit: int) -> str:
    if not limit:
        return ""
    # Sentence boundaries require whitespace: .NET, .moat and ASP.NET are words.
    sentence = re.split(r"(?<=[.!?])\s+", description.strip(), maxsplit=1)[0]
    return textwrap.shorten(sentence, width=limit, placeholder="…")


def _menu_entry(prefix: str, name: str, description: str, limit: int) -> str:
    provider_description = re.sub(r"(?<![\w/])[/$]gobby\b", lambda _: prefix, description)
    summary = _menu_description(provider_description, limit)
    return f"- `{prefix} {name}`" + (f" — {summary}" if summary else "")


def capability_menu(catalog: CapabilityCatalog, prefix: str, description_limit: int = 120) -> str:
    """Render names and metadata only, using the caller's provider syntax."""
    return "\n".join(
        _menu_entry(prefix, item.name, item.description, description_limit)
        for item in catalog.capabilities
    )


def standalone_menu(
    skills: Sequence[ParsedSkill],
    catalog: CapabilityCatalog,
    prefix: str,
    description_limit: int = 120,
) -> str:
    names = {item.name for item in catalog.capabilities}
    lines = []
    for skill in sorted(skills, key=lambda item: item.name):
        if skill.name == "gobby" or skill.is_always_apply() or skill.is_internal():
            continue
        invocation = f"skill {skill.name}" if skill.name in names else skill.name
        description = skill.description or ""
        # Language skills lead with their standards, then a long topic inventory.
        if "coding standards" in description:
            description = description.split(":", 1)[0]
        lines.append(_menu_entry(prefix, invocation, description, description_limit))
    return "\n".join(lines)


def topic_menu(capability: Capability, prefix: str) -> str:
    return "\n".join(
        f"- `{prefix} {capability.name} references {topic.name}` — "
        f"{topic.description}. Load when: {topic.when}."
        for topic in capability.topics
    )


def route_gobby_request(
    request: str,
    *,
    resolve_skill: Callable[[str], ParsedSkill | None],
    command_prefix: str = "/gobby",
    catalog: CapabilityCatalog | None = None,
) -> GobbyRoute:
    """Resolve arguments after the router trigger without executing operations.

    Agents evaluate natural-language loading conditions. The router exposes their
    metadata instead of guessing relevance from keywords or loading all bodies.
    """
    catalog = catalog if catalog is not None else load_capability_catalog()
    parts = request.strip().split(None, 1)
    name = parts[0] if parts else "help"
    arguments = parts[1] if len(parts) > 1 else ""
    if name.lower() == "help":
        return GobbyRoute("help", capability_menu(catalog, command_prefix))
    explicit_skill = name.lower() == "skill"
    if explicit_skill:
        parts = arguments.split(None, 1)
        if not parts:
            return GobbyRoute("help", capability_menu(catalog, command_prefix))
        name = parts[0]
        arguments = parts[1] if len(parts) > 1 else ""
    capability = next((item for item in catalog.capabilities if item.name == name.lower()), None)
    if capability is not None and not explicit_skill:
        parts = arguments.split(None, 1)
        if parts and parts[0].lower() == "references":
            if len(parts) == 1:
                return GobbyRoute("menu", topic_menu(capability, command_prefix))
            topic_name = parts[1].strip()
            topic = next((item for item in capability.topics if item.name == topic_name), None)
            if topic is None:
                return GobbyRoute(
                    "menu",
                    f"Unknown {capability.name} reference {topic_name!r}.\n\n"
                    + topic_menu(capability, command_prefix),
                )
            return GobbyRoute("reference", instruction_fetch_directive(f"gobby:{topic.path}"))
        context = instruction_fetch_directive(f"gobby:{capability.overview}")
        if arguments:
            context += (
                "\n\nBefore handling the original request, load each applicable topic below "
                'through schema-gated get_skill_file(name="gobby", path=...). '
                "Evaluate its loading condition against the request; follow every cursor to null. "
                "Tool schemas remain authoritative for parameters.\n"
            )
            context += "\n".join(f"- {topic.path}: {topic.when}" for topic in capability.topics)
        return GobbyRoute("reference", context, arguments)
    skill = resolve_skill(name)
    if skill is None:
        return GobbyRoute("unknown", "", arguments, name)
    metadata = skill.metadata.get("gobby", {}) if isinstance(skill.metadata, dict) else {}
    levels = metadata.get("levels", []) if isinstance(metadata, dict) else []
    first_argument = arguments.split(None, 1)[0] if arguments else None
    if first_argument and isinstance(levels, list) and first_argument in levels:
        parameters = json.dumps({"name": skill.name, "level": first_argument})
        return GobbyRoute(
            "skill",
            "Load and fully read the skill in its own outer tool result: "
            f'call_tool("gobby-skills", "get_skill", {parameters}). '
            "Follow page.next_cursor using only cursor until null. Then continue.",
            arguments,
        )
    return GobbyRoute("skill", instruction_fetch_directive(skill.name), arguments)
