"""Persistent seats keep standing directives apart from handoffs and shared memory."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.agent_models import AgentDefinitionBody

pytestmark = pytest.mark.unit

SHARED = Path(__file__).resolve().parents[2] / "src/gobby/install/shared"
AGENTS = SHARED / "workflows/agents"
HANDOFFS = SHARED / "skills/gobby/references/sessions/handoffs.md"
MEMORY = SHARED / "skills/gobby/references/memory/overview.md"

PERSISTENT_SEATS = (
    "orchestrator",
    "assistant",
    "triage",
    "archivist",
    "security-auditor",
    "consolidator",
    "release-manager",
    "recovery-steward",
    "reporter",
    "log-monitor",
    "metrics-monitor",
)

STANDING_DIRECTIVE_BLOCK = (
    "Keep `standing-directives.md` in your agent workspace session folder "
    "(named in your Agent Workspace context). When an operator directive with a trigger arrives, "
    "add an entry on arrival with its date, source, trigger and status (waiting, fired or done, "
    "with the done time). Re-read the file after every compaction and every relayed coordination "
    "digest, and check it on every status report. Cite the file's path in the references of every "
    "handoff you write, as a pointer only; never copy its entries into a handoff."
)

BACKEND_NAMES = ("tmux", "claude", "codex", "droid", "grok", "agy", "gemini")


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _definition(name: str) -> AgentDefinitionBody:
    data: dict[str, Any] = yaml.safe_load((AGENTS / f"{name}.yaml").read_text(encoding="utf-8"))
    return AgentDefinitionBody.model_validate(data)


def _definition_names() -> list[str]:
    stems = [path.stem for path in AGENTS.glob("*.yaml") if path.stem != "default"]
    return stems + [stem.replace("-", " ") for stem in stems]


@pytest.mark.parametrize("seat", PERSISTENT_SEATS)
def test_persistent_seat_prompts_carry_standing_directive_duty(seat: str) -> None:
    prompts = _definition(seat).prompts
    block = _normalize(STANDING_DIRECTIVE_BLOCK)

    assert block in _normalize(prompts.agent or "")
    if prompts.persona is not None:
        assert block in _normalize(prompts.persona)


def test_standing_directive_block_names_no_definition_or_backend() -> None:
    block = _normalize(STANDING_DIRECTIVE_BLOCK).lower()

    assert not [name for name in _definition_names() if name in block]
    assert not [name for name in BACKEND_NAMES if re.search(rf"\b{name}\b", block)]


def test_standing_directive_block_names_no_hardcoded_workspace_path() -> None:
    assert "/" not in _normalize(STANDING_DIRECTIVE_BLOCK)


@pytest.mark.parametrize("reference", [HANDOFFS, MEMORY])
def test_reference_states_standing_directive_demarcation_with_version(reference: Path) -> None:
    text = reference.read_text(encoding="utf-8")

    assert "standing-directives.md" in text
    assert re.search(r"^_Version: \d+\.\d+\.\d+_$", text, re.MULTILINE)
    assert re.search(r"gray area", text, re.IGNORECASE)


def test_handoff_reference_points_to_standing_directives_without_copying_them() -> None:
    text = HANDOFFS.read_text(encoding="utf-8")

    assert "a pointer to `standing-directives.md`, never a copy of it" in text


def test_memory_reference_excludes_seat_instructions_and_temporary_state() -> None:
    text = _normalize(MEMORY.read_text(encoding="utf-8"))

    assert "Never one seat's instructions, task progress or temporary state." in text
