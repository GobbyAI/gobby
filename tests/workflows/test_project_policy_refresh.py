"""Live project policy refresh through the real import and hook paths."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.receipt_effects import STAGED_EFFECTS_FIELD, apply_acknowledged_receipt
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.imports import sync_imported_workflow_file
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import sync_rule_file
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.integration


def _import_policy(
    db: HubDatabase, path: Path, project_id: str, entrypoint: Literal["sync", "import"]
) -> None:
    if entrypoint == "sync":
        result = sync_rule_file(db, path, project_id=project_id)
    else:
        result = sync_imported_workflow_file(db, path, project_id)
    assert result["errors"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["sync", "import"])
@pytest.mark.parametrize("changed", [True, False], ids=["changed", "unchanged"])
async def test_live_policy_refreshes_once_after_import(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    entrypoint: Literal["sync", "import"],
    changed: bool,
) -> None:
    checkout = isolated_checkout_factory(temp_db, "policy-refresh")
    sessions = SessionManager(temp_db)
    session_id = sessions.register_session(
        external_id="policy-refresh-live",
        machine_id=checkout.machine_id,
        source=SessionSource.CLAUDE.value,
        project_id=checkout.project.id,
        project_path=checkout.root_path,
    )
    assert session_id
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(session_id, {"_agent_type": "test-writer"})
    handler = WorkflowHookHandler(rule_engine=RuleEngine(temp_db), session_manager=sessions)

    async def turn() -> HookResponse:
        response = await handler.evaluate_async(
            HookEvent(
                event_type=HookEventType.BEFORE_AGENT,
                session_id="policy-refresh-live",
                source=SessionSource.CLAUDE,
                timestamp=datetime.now(UTC),
                data={},
                cwd=checkout.root_path,
                project_id=checkout.project.id,
                metadata={"_platform_session_id": session_id, "project_path": checkout.root_path},
            )
        )
        assert response.decision == "allow"
        staged = response.metadata.get(STAGED_EFFECTS_FIELD)
        if staged:
            apply_acknowledged_receipt(
                SimpleNamespace(
                    session_id=session_id, staged_payload=staged, receipt_id="policy-ack"
                ),
                variable_manager=variables,
            )
        return response

    shipped = (
        Path(__file__).resolve().parents[2] / ".gobby/workflows/rules/project-operating-policy.yaml"
    )
    document: dict[str, Any] = yaml.safe_load(shipped.read_text(encoding="utf-8"))
    path = tmp_path / "project-operating-policy.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    _import_policy(temp_db, path, checkout.project.id, entrypoint)

    first = await turn()
    assert "Project Operating Policy" in (first.context or "")
    initial = variables.get_variables(session_id)
    assert initial["_project_policy_injected"] is True
    assert initial["project_policy"]["operator"] == "Josh"
    assert "Project Operating Policy" not in ((await turn()).context or "")

    if changed:
        effects = document["rules"]["project-operating-policy"]["effects"]
        effects[0]["value"]["operator"] = "Updated policy operator"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
    _import_policy(temp_db, path, checkout.project.id, entrypoint)

    refreshed = await turn()
    expected_operator = "Updated policy operator" if changed else "Josh"
    assert variables.get_variables(session_id)["project_policy"]["operator"] == expected_operator
    assert ("Project Operating Policy" in (refreshed.context or "")) is changed
    assert "Project Operating Policy" not in ((await turn()).context or "")
