"""Optional AI synthesis for project verification refresh."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from gobby.ai.text_generation import TextGenerateJSONAdapter, TextGenerationRequest
from gobby.config.features import ProjectVerificationSynthesisConfig
from gobby.project_verification.candidates import (
    CommandCandidate,
    command_evidence_key,
    is_safe_validation_command,
)
from gobby.project_verification.evidence import STANDARD_SLOTS, EvidenceBundle

CALLER = "project_verification.refresh"

SYSTEM_PROMPT = """You select project verification commands from evidenced candidates.
Return only JSON. Do not invent commands. Prefer commands supported by CI,
manifests, package scripts, Make/Just/Task recipes, validation docs, or existing
.gobby/project.json entries. Reject mutating validation forms such as formatters
without --check or commands with --fix."""

_COMMAND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "sources": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["command", "confidence", "sources", "rationale"],
    "additionalProperties": False,
}

PROJECT_VERIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "commands": {"type": "object", "additionalProperties": _COMMAND_SCHEMA},
        "custom": {"type": "object", "additionalProperties": _COMMAND_SCHEMA},
    },
    "required": ["commands"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class RejectedCommand:
    """One rejected AI command entry."""

    name: str
    command: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "command": self.command, "reason": self.reason}


@dataclass
class SynthesisResult:
    """Validated synthesis output."""

    accepted: dict[str, CommandCandidate] = field(default_factory=dict)
    rejected: list[RejectedCommand] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_accepted(self) -> bool:
        return bool(self.accepted)


async def synthesize_verification_commands(
    service: TextGenerateJSONAdapter,
    config: ProjectVerificationSynthesisConfig,
    bundle: EvidenceBundle,
    candidates: list[CommandCandidate],
) -> SynthesisResult:
    """Ask a text-generation service to choose from evidenced candidates."""
    request = TextGenerationRequest(
        prompt=_build_prompt(bundle, candidates, config.confidence_threshold),
        profile=str(config.profile),
        candidates=tuple(config.candidates),
        system_prompt=SYSTEM_PROMPT,
        json_schema=PROJECT_VERIFICATION_SCHEMA,
        caller=CALLER,
        cwd=str(bundle.root),
    )
    raw = await service.generate_json(request)
    return validate_synthesis(raw, config, candidates)


def validate_synthesis(
    raw: dict[str, Any],
    config: ProjectVerificationSynthesisConfig,
    candidates: list[CommandCandidate],
) -> SynthesisResult:
    """Validate strict AI JSON against deterministic candidate evidence."""
    result = SynthesisResult(raw=raw)
    by_key = {
        command_evidence_key(candidate.name, candidate.command): candidate
        for candidate in candidates
    }

    for name, entry in _iter_command_entries(raw):
        command = entry.get("command")
        confidence = entry.get("confidence")
        rationale = entry.get("rationale")
        if not isinstance(command, str) or not command.strip():
            result.rejected.append(RejectedCommand(name, str(command), "missing command"))
            continue
        if not isinstance(confidence, int | float):
            result.rejected.append(RejectedCommand(name, command, "missing numeric confidence"))
            continue
        if confidence < config.confidence_threshold:
            result.rejected.append(RejectedCommand(name, command, "below confidence threshold"))
            continue
        if name not in STANDARD_SLOTS and name not in {candidate.name for candidate in candidates}:
            result.rejected.append(RejectedCommand(name, command, "unsupported command name"))
            continue
        if not is_safe_validation_command(command):
            result.rejected.append(RejectedCommand(name, command, "mutating validation command"))
            continue

        candidate = by_key.get(command_evidence_key(name, command))
        if candidate is None:
            result.rejected.append(
                RejectedCommand(name, command, "command lacks deterministic evidence")
            )
            continue
        accepted_confidence = max(float(confidence), candidate.confidence)
        result.accepted[name] = CommandCandidate(
            name=candidate.name,
            slot=candidate.slot,
            command=candidate.command,
            confidence=accepted_confidence,
            source="ai",
            source_kind="ai",
            rationale=str(rationale or candidate.rationale),
            custom=candidate.custom,
        )
    return result


def _build_prompt(
    bundle: EvidenceBundle,
    candidates: list[CommandCandidate],
    confidence_threshold: float,
) -> str:
    payload = {
        "task": "Select verification commands for .gobby/project.json.",
        "schema": {
            "commands": {
                "<name>": {
                    "command": "<exact candidate command>",
                    "confidence": "number between 0 and 1",
                    "sources": ["evidence source names"],
                    "rationale": "brief reason",
                }
            }
        },
        "rules": [
            "Use only exact commands from candidates.",
            f"Use confidence >= {confidence_threshold}.",
            "Use standard names when applicable; keep frontend/manual entries as custom names.",
            "Do not return mutating commands such as --fix or formatter writes.",
        ],
        "evidence": bundle.to_prompt_dict(),
        "candidates": [candidate.to_prompt_dict() for candidate in candidates],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _iter_command_entries(raw: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    commands = raw.get("commands", {})
    if isinstance(commands, dict):
        entries.extend(_entries_from_mapping(commands))
    custom = raw.get("custom", {})
    if isinstance(custom, dict):
        entries.extend(_entries_from_mapping(custom))
    return entries


def _entries_from_mapping(mapping: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    for name, entry in mapping.items():
        if isinstance(entry, dict):
            entries.append((str(name), entry))
        elif isinstance(entry, str):
            entries.append((str(name), {"command": entry, "confidence": 0.0}))
        else:
            entries.append((str(name), {"command": str(entry), "confidence": 0.0}))
    return entries
