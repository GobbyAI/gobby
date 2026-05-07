"""Discovery-stage artifact checks for dispatcher-driven completion."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DiscoveryArtifactSpec:
    """Marker and heading requirements for a discovery stage artifact."""

    marker_name: str
    required_headings: tuple[str, ...]


DISCOVERY_ARTIFACT_SPECS: dict[str, DiscoveryArtifactSpec] = {
    "ideation": DiscoveryArtifactSpec(
        marker_name="ideation",
        required_headings=(
            "## Discovery Brief",
            "### Problem",
            "### Constraints",
            "### Hypotheses",
            "### Open Questions",
        ),
    ),
    "research": DiscoveryArtifactSpec(
        marker_name="research",
        required_headings=(
            "## Research Findings",
            "### Research Questions",
            "### Domain Context",
            "### Evidence & Sources",
            "### Risks",
        ),
    ),
    "architecture": DiscoveryArtifactSpec(
        marker_name="architecture",
        required_headings=(
            "## Architecture Brief",
            "### Drivers",
            "### Decisions",
            "### Components",
            "### Interfaces",
            "### Trade-offs",
            "### Open Questions",
            "## Test Architecture",
        ),
    ),
    "prd": DiscoveryArtifactSpec(
        marker_name="prd",
        required_headings=(
            "## Product Reference Document",
            "### Goal",
            "### Users",
            "### Scope",
            "### Out of Scope",
            "### Acceptance Criteria",
            "### Planning Handoff",
        ),
    ),
}


def discovery_artifact_ready(task: object, stage_name: str) -> bool:
    """Return true when task description contains the completed stage artifact."""

    spec = DISCOVERY_ARTIFACT_SPECS.get(stage_name)
    if spec is None:
        return False
    description = _field(task, "description")
    if not isinstance(description, str) or not description:
        return False
    block = _marker_block(description, spec.marker_name)
    if block is None:
        return False
    content = block.strip()
    if not content:
        return False
    return all(content.count(heading) == 1 for heading in spec.required_headings)


def _marker_block(description: str, marker_name: str) -> str | None:
    start = f"<!-- gobby:discovery-stage:{marker_name}:start -->"
    end = f"<!-- gobby:discovery-stage:{marker_name}:end -->"
    start_index = description.find(start)
    end_index = description.find(end)
    if start_index < 0 or end_index < 0 or end_index <= start_index:
        return None
    if description.find(start, start_index + len(start)) >= 0:
        return None
    if description.find(end, end_index + len(end)) >= 0:
        return None
    return description[start_index + len(start) : end_index]


def _field(obj: object, name: str) -> object:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


__all__ = [
    "DISCOVERY_ARTIFACT_SPECS",
    "DiscoveryArtifactSpec",
    "discovery_artifact_ready",
]
