"""A bundled agent definition can record the handoff-continuation e2e pass.

The closure gate is satisfied only by an approved record of this kind on the candidate
SHA. A definition that grants the kind must also declare it, or the writer refuses the
record. The test enumerates bundled definitions generically and names none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.workflows.agent_models import AgentDefinitionBody

pytestmark = pytest.mark.unit

_E2E_KIND = "handoff-continuation-e2e"
_AGENT_YAMLS = sorted(get_bundled_agents_path().rglob("*.yaml"))


def _definition(path: Path) -> AgentDefinitionBody:
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} is not a YAML mapping"
    name = data.get("name")
    data["name"] = name.strip() if isinstance(name, str) and name.strip() else path.stem
    return AgentDefinitionBody.model_validate(data)


@pytest.mark.parametrize("path", _AGENT_YAMLS, ids=lambda path: path.name)
def test_definition_granting_the_e2e_kind_declares_it_with_an_approved_result(path: Path) -> None:
    body = _definition(path)
    if _E2E_KIND not in body.task_attestation_writes:
        return
    declared = body.task_attestation_kinds.get(_E2E_KIND)
    assert declared is not None, f"{path.name} grants {_E2E_KIND} but does not declare it"
    assert "approved" in declared.allowed_results


def test_a_bundled_definition_can_record_the_e2e_pass() -> None:
    recorders = [
        path.name for path in _AGENT_YAMLS if _E2E_KIND in _definition(path).task_attestation_writes
    ]
    assert recorders, f"no bundled definition can record {_E2E_KIND}"
