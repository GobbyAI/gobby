"""Validated, metadata-only catalog for Gobby capability routing."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gobby.skills.instruction_requirements import parse_instruction_requirement


class _CatalogRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    when: str

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        requirement = parse_instruction_requirement(value)
        if requirement.path is not None:
            raise ValueError("Catalog names must be plain identifiers")
        return value

    @field_validator("description", "when")
    @classmethod
    def nonempty_metadata(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Catalog descriptions and loading conditions must be nonempty")
        return value


class CapabilityTopic(_CatalogRecord):
    path: str


class Capability(_CatalogRecord):
    overview: str
    topics: tuple[CapabilityTopic, ...]


class CapabilityCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    capabilities: tuple[Capability, ...]
    folded_skills: dict[str, str] = Field(default_factory=dict)

    @field_validator("version", mode="before")
    @classmethod
    def valid_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported capability catalog version; expected integer 1")
        return value


def validate_capability_catalog(catalog: CapabilityCatalog, skill_root: Path) -> None:
    """Reject ambiguous routing and references outside the selected skill directory."""
    root = skill_root.resolve()
    names: set[str] = set()
    identities: set[str] = set()
    for capability in catalog.capabilities:
        if capability.name in names:
            raise ValueError(f"Duplicate capability name: {capability.name}")
        names.add(capability.name)
        topic_names: set[str] = set()
        paths = [(f"{capability.name} overview", capability.overview)]
        for topic in capability.topics:
            if topic.name in topic_names:
                raise ValueError(f"Duplicate topic identifier: {capability.name}/{topic.name}")
            topic_names.add(topic.name)
            paths.append((f"{capability.name}/{topic.name}", topic.path))
        for label, path in paths:
            try:
                identity = parse_instruction_requirement(f"gobby:{path}").identity
            except ValueError as exc:
                raise ValueError(f"{label}: {exc}") from exc
            if identity in identities:
                raise ValueError(f"{label}: duplicate reference identity {identity}")
            identities.add(identity)
            target = (root / path).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"{label}: reference escapes skill directory: {path}")
            if not target.is_file():
                raise ValueError(f"{label}: missing reference file: {path}")

    for name, destination in catalog.folded_skills.items():
        if parse_instruction_requirement(name).path is not None:
            raise ValueError(f"Folded skill name must be a plain identifier: {name}")
        if destination not in identities:
            raise ValueError(f"Folded skill {name}: unknown reference identity {destination}")


def _unique_catalog_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate catalog key: {key}")
        result[key] = value
    return result


def load_capability_catalog(skill_root: Path | None = None) -> CapabilityCatalog:
    """Load catalog metadata and check its files without loading instruction bodies."""
    if skill_root is None:
        from gobby.skills.sync import get_bundled_skills_path

        skill_root = get_bundled_skills_path() / "gobby"
    path = skill_root / "catalog.json"
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_catalog_object
        )
        catalog = CapabilityCatalog.model_validate(data)
        validate_capability_catalog(catalog, skill_root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid capability catalog {path}: {exc}") from exc
    return catalog
