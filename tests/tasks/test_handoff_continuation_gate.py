"""A handoff-continuation candidate lands and closes only with an e2e pass on its exact SHA.

The path list and the requirement live in bundled closure YAML. This test selects
the bundled rules that reference the e2e attestation kind and evaluates them through
the closure engine; it never names a bundled rule.
"""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml

from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.tasks.attestations import TaskAttestation
from gobby.tasks.closure_policy import ClosureDecision, ClosureSnapshot, evaluate_closure_policy
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.rule_validation_yaml import normalize_rule_yaml_entry
from gobby.workflows.sync_rules import get_bundled_rules_path

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_E2E_KIND = "handoff-continuation-e2e"
_GATE_TAG = "synthetic-handoff-gate"
_HANDOFF_PATH = "src/gobby/hooks/compaction_ownership.py"
_UNRELATED_PATH = "src/gobby/tasks/closure_policy.py"
_CANDIDATE = "a" * 40
_OTHER_CANDIDATE = "b" * 40


def _gate_rows() -> list[RuleDefinitionRow]:
    """Every bundled closure rule whose condition consults the e2e attestation kind.

    Each entry is normalized the way bundled sync normalizes it, so the closure engine
    sees the persisted body rather than authoring-only keys. Every row carries the synthetic
    selector tag, so the lander selects the bundled gate without naming its bundled group.
    """
    rows: list[RuleDefinitionRow] = []
    for path in sorted((get_bundled_rules_path() / "task-closure").glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        tags = [_GATE_TAG]
        for name, rule in (document.get("rules") or {}).items():
            if _E2E_KIND not in str(rule.get("when", "")):
                continue
            definition_data = {
                "description": rule.get("description"),
                "enabled": rule.get("enabled", True),
                "priority": rule.get("priority", 100),
                **rule,
                "tags": tags,
            }
            body, metadata = normalize_rule_yaml_entry(definition_data)
            rows.append(
                RuleDefinitionRow(
                    id=name,
                    name=name,
                    enabled=bool(metadata.get("enabled", True)),
                    enabled_pinned=False,
                    priority=int(metadata["priority"]),
                    definition_json=body,
                    tags=tags,
                    source="installed",
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )
    return rows


def _lander() -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(
        {
            "name": "arbitrary-lander",
            "prompts": {"agent": "Land the task."},
            "workflows": {"rule_selectors": {"include": [f"tag:{_GATE_TAG}"]}},
        }
    )


def _e2e_record(sequence: int, **updates: Any) -> TaskAttestation:
    return TaskAttestation.model_validate(
        {
            "id": f"e2e-record-{sequence}",
            "task_id": "task-1",
            "kind": _E2E_KIND,
            "result": "approved",
            "summary": "Handoff continuation e2e passed on the candidate.",
            "session_id": "e2e-runner",
            "agent_definition": "arbitrary-runner",
            "agent_definition_hash": "digest",
            "agent_capabilities": [],
            "sequence": sequence,
            "created_at": _NOW,
            "reference": {"scheme": "git", "value": _CANDIDATE},
            **updates,
        }
    )


def _decide(
    event: Literal["task_close", "task_land"],
    paths: list[str],
    records: list[TaskAttestation],
) -> ClosureDecision:
    snapshot = ClosureSnapshot(
        event=event,
        task={"id": "task-1", "category": "test", "labels": []},
        reason="done",
        attestations=tuple(records),
        facts={
            "candidate_commit_sha": _CANDIDATE,
            "candidate_paths": paths,
            "candidate_paths_available": True,
            "project_policy_available": True,
            "project_policy": {},
        },
    )
    return evaluate_closure_policy(snapshot, agent=_lander(), rules=_gate_rows())


def _declared_paths() -> list[str]:
    """Every surface path the bundled e2e gate names, read from the closure YAML conditions."""
    paths: set[str] = set()
    for path in sorted((get_bundled_rules_path() / "task-closure").glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for rule in (document.get("rules") or {}).values():
            when = str(rule.get("when", ""))
            if _E2E_KIND in when:
                paths.update(re.findall(r"'(src/[^']+)'", when))
    return sorted(paths)


def test_declared_handoff_path_is_a_real_file() -> None:
    assert (_REPO_ROOT / _HANDOFF_PATH).is_file()


def test_every_declared_handoff_path_matches_a_real_file() -> None:
    paths = _declared_paths()
    assert paths
    for path in paths:
        assert any(_REPO_ROOT.glob(path)), f"declared handoff path {path} matches no file"


@pytest.mark.parametrize(
    "surface",
    [
        "src/gobby/mcp_proxy/tools/sessions/_terminal.py",
        "src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py",
    ],
)
def test_terminal_set_handoff_and_clear_dispatch_are_declared(surface: str) -> None:
    assert surface in _declared_paths()


def test_gate_rows_cover_landing_and_closure() -> None:
    rows = _gate_rows()
    assert {row.definition_json.get("event") for row in rows} == {"task_land", "task_close"}


@pytest.mark.parametrize(
    ("event", "paths", "records", "allowed"),
    [
        ("task_land", [_HANDOFF_PATH], [], False),
        ("task_close", [_HANDOFF_PATH], [], False),
        ("task_land", [_HANDOFF_PATH], [_e2e_record(1)], True),
        ("task_close", [_HANDOFF_PATH], [_e2e_record(1)], True),
        (
            "task_land",
            [_HANDOFF_PATH],
            [_e2e_record(1, reference={"scheme": "git", "value": _OTHER_CANDIDATE})],
            False,
        ),
        ("task_land", [_HANDOFF_PATH], [_e2e_record(1, result="rejected")], False),
        ("task_land", [_UNRELATED_PATH], [], True),
        (
            "task_land",
            ["src/gobby/mcp_proxy/tools/sessions/_terminal.py"],
            [],
            False,
        ),
        (
            "task_close",
            ["src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py"],
            [],
            False,
        ),
    ],
)
def test_e2e_pass_must_name_the_exact_candidate_for_handoff_paths(
    event: Literal["task_close", "task_land"],
    paths: list[str],
    records: list[TaskAttestation],
    allowed: bool,
) -> None:
    decision = _decide(event, paths, records)
    assert decision.allowed is allowed, decision.blockers
