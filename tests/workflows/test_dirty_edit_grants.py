"""Operator approvals for unowned dirty paths: recording, expiry and who may write them.

Also covers the edit ledgers the gate trusts, which no seat may write either.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import httpx
import pytest
import yaml
from click.testing import CliRunner

from gobby.cli.sessions_dirty_paths import approve_dirty_paths
from gobby.mcp_proxy.tools.workflows._variables import set_variable
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.utils import local_token
from gobby.utils.local_token import GOBBY_AGENT_API_TOKEN_ENV, GOBBY_MANAGED_EXECUTION_ID_ENV
from gobby.workflows.dirty_edit_grants import (
    OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE,
    approved_dirty_paths,
    record_dirty_path_approval,
)
from gobby.workflows.engine.command_matching import command_patterns_match
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def seat(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase, tmp_path: Path
) -> Session:
    project = isolated_checkout_factory(temp_db, "dirty-approval-test", root=tmp_path).project
    return SessionManager(temp_db).register(
        external_id="seat-external",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project.id,
    )


def _approve(db: HubDatabase, seat: Session, root: Path, paths: list[str]) -> set[str]:
    record_dirty_path_approval(
        db, seat.id, checkout_root=str(root), paths=paths, approved_by="operator"
    )
    return approved_dirty_paths(SessionVariableManager(db).get_variables(seat.id), str(root))


def test_dirty_path_approval_records_exact_checkout_relative_paths(
    temp_db: HubDatabase, seat: Session, tmp_path: Path
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("orphaned\n", encoding="utf-8")

    approved = _approve(temp_db, seat, tmp_path, ["src/a.py", str(tmp_path / "b.txt")])
    variables = SessionVariableManager(temp_db).get_variables(seat.id)

    assert approved == {"src/a.py", "b.txt"}
    assert approved_dirty_paths(variables, str(tmp_path / "src")) == set()
    for outside in ("../escape.txt", str(tmp_path.parent / "escape.txt"), ".", "", "src"):
        with pytest.raises(ValueError):
            record_dirty_path_approval(
                temp_db,
                seat.id,
                checkout_root=str(tmp_path),
                paths=[outside],
                approved_by="operator",
            )


def test_dirty_path_approval_lapses_when_path_content_changes(
    temp_db: HubDatabase, seat: Session, tmp_path: Path
) -> None:
    (tmp_path / "edited.txt").write_text("approved content\n", encoding="utf-8")
    (tmp_path / "removed.txt").write_text("approved content\n", encoding="utf-8")

    approved = _approve(temp_db, seat, tmp_path, ["edited.txt", "removed.txt", "created.txt"])
    (tmp_path / "edited.txt").write_text("changed after approval\n", encoding="utf-8")
    (tmp_path / "removed.txt").unlink()
    (tmp_path / "created.txt").write_text("appeared after approval\n", encoding="utf-8")
    variables = SessionVariableManager(temp_db).get_variables(seat.id)

    assert approved == {"edited.txt", "removed.txt", "created.txt"}
    assert approved_dirty_paths(variables, str(tmp_path)) == set()


@pytest.mark.parametrize("scope", ["session", "step"])
def test_seat_variable_writes_cannot_create_a_dirty_path_approval(
    temp_db: HubDatabase, seat: Session, tmp_path: Path, scope: Literal["session", "step"]
) -> None:
    forged = {str(tmp_path): {"paths": ["a.txt"], "approved_by": "seat", "approved_at": "now"}}

    result = set_variable(
        SessionManager(temp_db),
        temp_db,
        name=OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE,
        value=forged,
        session_id=seat.id,
        scope=scope,
    )
    variables = SessionVariableManager(temp_db).get_variables(seat.id)

    assert result["success"] is False
    assert OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE not in variables


@pytest.mark.parametrize(
    ("name", "forged"),
    [
        ("session_dirty_files", ["a.txt"]),
        ("session_dirty_file_checkouts", {"/repo": ["a.txt"]}),
        ("task_edited_file_checkouts", {"task-id": {"/repo": ["a.txt"]}}),
    ],
)
def test_seat_variable_writes_cannot_forge_the_edit_ledgers_the_gate_trusts(
    temp_db: HubDatabase, seat: Session, name: str, forged: list[str] | dict[str, Any]
) -> None:
    result = set_variable(
        SessionManager(temp_db), temp_db, name=name, value=forged, session_id=seat.id
    )
    variables = SessionVariableManager(temp_db).get_variables(seat.id)

    assert result["success"] is False
    assert variables.get(name) != forged


RULE_FILE = get_bundled_rules_path() / "task-enforcement" / "block-dirty-path-approval-cli.yaml"
OBFUSCATED_SPELLINGS = (
    'a=approve-dirty-; b=paths; c=sess; d=ions; "{python}" -m gobby.cli $c$d $a$b s7 '
    "--checkout {root} a.txt",
    "printf '\"{python}\" -m gobby.cli sessions approve-dirty-%s s7 --checkout {root} a.txt\\n' "
    "paths | sh",
)


@pytest.fixture
def bootstrap_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated gobby home: the CLI reads no real operator key and reaches no daemon."""
    home = tmp_path / "gobby-home"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setenv("GOBBY_DAEMON_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(local_token, "_daemon_bootstrap", None)
    return home


def _write_operator_key(home: Path, key: str) -> None:
    (home / "bootstrap.yaml").write_text(f"api_key: {key!r}\n", encoding="utf-8")


def _rule_blocks(command: str) -> bool:
    rules = yaml.safe_load(RULE_FILE.read_text(encoding="utf-8"))["rules"]
    return any(
        command_patterns_match(
            command, pattern=effect["command_pattern"], mask_quoted=effect["mask_quoted"]
        )
        for rule in rules.values()
        for effect in rule["effects"]
    )


def test_approve_dirty_paths_cli_refuses_without_an_operator_key(
    temp_db: HubDatabase, seat: Session, tmp_path: Path, bootstrap_home: Path
) -> None:
    with patch("gobby.cli.sessions_dirty_paths.httpx.post") as post:
        result = CliRunner().invoke(
            approve_dirty_paths, [seat.id, "--checkout", str(tmp_path), "a.txt"]
        )
    variables = SessionVariableManager(temp_db).get_variables(seat.id)

    assert result.exit_code != 0
    assert "gobby auth login" in result.output
    post.assert_not_called()
    assert OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE not in variables


@pytest.mark.parametrize("bootstrap_key", [None, "   "], ids=["absent", "blank"])
def test_approve_dirty_paths_cli_ignores_managed_credentials_without_an_operator_key(
    tmp_path: Path,
    bootstrap_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    bootstrap_key: str | None,
) -> None:
    if bootstrap_key is not None:
        _write_operator_key(bootstrap_home, bootstrap_key)
    monkeypatch.setenv(GOBBY_AGENT_API_TOKEN_ENV, "managed-token")
    monkeypatch.setenv(GOBBY_MANAGED_EXECUTION_ID_ENV, "managed-execution")
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", str(bootstrap_home / "managed.yaml"))
    with patch("gobby.cli.sessions_dirty_paths.httpx.post") as post:
        result = CliRunner().invoke(
            approve_dirty_paths, ["seat-ref", "--checkout", str(tmp_path), "a.txt"]
        )

    assert result.exit_code != 0
    assert "gobby auth login" in result.output
    post.assert_not_called()


def test_approve_dirty_paths_cli_sends_only_the_operator_key(
    seat: Session, tmp_path: Path, bootstrap_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_operator_key(bootstrap_home, "operator-key")
    monkeypatch.setenv(GOBBY_AGENT_API_TOKEN_ENV, "managed-token")
    monkeypatch.setenv(GOBBY_MANAGED_EXECUTION_ID_ENV, "managed-execution")
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", str(bootstrap_home / "managed.yaml"))
    requests: list[httpx.Request] = []
    post_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"session_id": seat.id, "approvals": {"a.txt": "f1"}})

    def through_transport(url: str, **kwargs: Any) -> httpx.Response:
        post_calls.append(kwargs)
        with httpx.Client(
            transport=httpx.MockTransport(handler), trust_env=kwargs["trust_env"]
        ) as client:
            return client.post(url, json=kwargs["json"], headers=kwargs["headers"])

    with patch("gobby.cli.sessions_dirty_paths.httpx.post", side_effect=through_transport):
        result = CliRunner().invoke(
            approve_dirty_paths, [seat.id, "--checkout", str(tmp_path), "a.txt"]
        )

    assert result.exit_code == 0, result.output
    assert "  a.txt (f1)" in result.output
    [call] = post_calls
    assert call["trust_env"] is False
    assert call["headers"] == {"Authorization": "Bearer operator-key"}
    [request] = requests
    assert request.headers.get_list("authorization") == ["Bearer operator-key"]
    assert not [name for name in request.headers if name.lower().startswith("x-gobby")]
    assert request.url.path == f"/api/sessions/{seat.id}/dirty-path-approvals"


def test_approve_dirty_paths_cli_reports_the_daemon_refusal(
    seat: Session, tmp_path: Path, bootstrap_home: Path
) -> None:
    _write_operator_key(bootstrap_home, "operator-key")

    def refuse(url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        return httpx.Response(400, json={"detail": "not an absolute path"}, request=request)

    with patch("gobby.cli.sessions_dirty_paths.httpx.post", side_effect=refuse):
        result = CliRunner().invoke(
            approve_dirty_paths, [seat.id, "--checkout", str(tmp_path), "a"]
        )

    assert result.exit_code != 0
    assert "approval refused (400): not an absolute path" in result.output


@pytest.mark.parametrize("spelling", OBFUSCATED_SPELLINGS, ids=["variable-built", "printf-piped"])
def test_obfuscated_approval_spellings_pass_the_rule_and_stop_at_the_cli(
    spelling: str, tmp_path: Path, bootstrap_home: Path
) -> None:
    command = spelling.format(python=sys.executable, root=tmp_path)
    assert not _rule_blocks(command)

    result = subprocess.run(
        ["sh", "-c", command],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "GOBBY_HOME": str(bootstrap_home),
            "GOBBY_DAEMON_URL": "http://127.0.0.1:1",
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode != 0
    assert "gobby auth login" in result.stderr
