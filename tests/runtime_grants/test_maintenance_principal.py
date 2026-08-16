"""Maintenance grant principal wire value and token kind."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from gobby.runtime_grants.schema import GrantPrincipal
from gobby.utils.local_token import (
    issue_maintenance_api_token,
    verify_agent_api_token,
)

pytestmark = pytest.mark.unit


def test_maintenance_principal_serializes_as_snake_case_kind() -> None:
    principal = GrantPrincipal(
        kind="maintenance",
        machine_id="machine-1",
        project_id="project-1",
        execution_id="exec-1",
        session_id=None,
    )

    dumped = json.loads(principal.model_dump_json())

    assert dumped["kind"] == "maintenance"
    assert dumped["execution_id"] == "exec-1"
    assert dumped["session_id"] is None


def test_maintenance_kind_has_no_compatibility_alias() -> None:
    with pytest.raises(ValidationError):
        GrantPrincipal(
            kind="maint",
            machine_id="machine-1",
            project_id="project-1",
            execution_id="exec-1",
            session_id=None,
        )


def test_issue_maintenance_api_token_embeds_kind_and_execution() -> None:
    token = issue_maintenance_api_token(
        "operator-token",
        execution_id="exec-1",
        project_id="project-1",
        machine_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        timeout_seconds=30,
    )
    claims = verify_agent_api_token(token, "operator-token")

    assert claims is not None
    assert claims.kind == "maintenance"
    assert claims.managed_execution_id == "exec-1"
    assert claims.agent_run_id is None
    assert claims.project_id == "project-1"


def test_maintenance_token_does_not_verify_as_tool_chat_principal() -> None:
    token = issue_maintenance_api_token(
        "operator-token",
        execution_id="exec-1",
        project_id="project-1",
        machine_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        timeout_seconds=30,
    )
    claims = verify_agent_api_token(token, "operator-token")
    assert claims is not None
    assert claims.kind != "tool_chat"
    payload: dict[str, Any] = {
        "kind": claims.kind,
        "managed_execution_id": claims.managed_execution_id,
    }
    assert payload["kind"] == "maintenance"
