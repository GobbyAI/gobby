"""`gobby tokens ledger` CLI: scope totals, JSON parity and distinct unknown spend (plan 3.2, #23602)."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from click.testing import CliRunner

from gobby.sessions.reported_usage import ReportedRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.reported_usage import ReportedUsageStore
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.token_events import TokenEvent, TokenEventStore
from gobby.storage.usage_ledger import UsageLedgerStore
from gobby.utils.machine_id import get_machine_id
from tests.fixtures.postgres import TEST_USER_ID

tokens_module = importlib.import_module("gobby.cli.tokens")

pytestmark = pytest.mark.integration

BASE = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cli_db(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> HubDatabase:
    monkeypatch.setattr(tokens_module, "require_cli_database", lambda ctx=None: temp_db)
    monkeypatch.setattr(tokens_module, "resolve_session_id", lambda ref, project_id=None: ref)
    return temp_db


def _session(
    session_manager: SessionManager, project_id: str, name: str, source: str = "claude"
) -> str:
    machine_id = get_machine_id()
    assert machine_id is not None
    LocalMachineManager(session_manager.db).upsert_seen(machine_id, TEST_USER_ID)
    return session_manager.register(
        external_id=f"tokens-ledger-{name}",
        machine_id=machine_id,
        source=source,
        project_id=project_id,
    ).id


def _event(
    db: HubDatabase,
    project_id: str,
    session_id: str,
    *,
    at: datetime,
    message_id: str,
    source: str = "claude",
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> None:
    TokenEventStore(db).record(
        TokenEvent(
            session_id=session_id,
            project_id=project_id,
            message_id=message_id,
            source=source,
            origin="transcript",
            model=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            event_at=at,
            api_calls=1,
        )
    )


def _interval(
    db: HubDatabase,
    task_id: str,
    session_id: str,
    claimed_at: datetime,
    released_at: datetime,
) -> None:
    db.execute(
        """
        INSERT INTO task_claim_intervals (task_id, session_id, claimed_at, released_at)
        VALUES (%s, %s, %s, %s)
        """,
        (task_id, session_id, claimed_at, released_at),
    )


def _run(
    run_key: str,
    *,
    source: str,
    started: datetime,
    observed: datetime,
    amount: str | None = None,
    complete: bool = True,
) -> ReportedRun:
    return ReportedRun(
        run_key=run_key,
        source=source,
        cost_unit=None if amount is None else "usd",
        cost_amount=None if amount is None else Decimal(amount),
        cost_complete=complete,
        api_duration_ms=None,
        wall_duration_ms=None,
        started_at=started,
        observed_at=observed,
    )


def test_ledger_command_prints_scope_totals(
    cli_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    runner: CliRunner,
) -> None:
    project_id = sample_project["id"]
    session_id = _session(session_manager, project_id, "scope")
    _event(cli_db, project_id, session_id, at=BASE + timedelta(minutes=1), message_id="msg-1")
    _event(
        cli_db,
        project_id,
        session_id,
        at=BASE + timedelta(minutes=2),
        message_id="msg-2",
        input_tokens=20,
        output_tokens=7,
    )
    task_id = (
        LocalTaskManager(cli_db)
        .create_task(project_id, "Ledger scope", validation_criteria="Scope follows.")
        .id
    )
    _interval(cli_db, task_id, session_id, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
    expected = "totals: in=30 out=12 cache_write=0 cache_read=0"

    by_session = runner.invoke(tokens_module.tokens, ["ledger", "--session", session_id])
    assert by_session.exit_code == 0, by_session.output
    assert expected in by_session.output.splitlines()

    by_task = runner.invoke(tokens_module.tokens, ["ledger", "--task", task_id])
    assert by_task.exit_code == 0, by_task.output
    assert expected in by_task.output.splitlines()

    as_json = runner.invoke(tokens_module.tokens, ["ledger", "--session", session_id, "--json"])
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.output) == UsageLedgerStore(cli_db).page(session_id=session_id)

    both = runner.invoke(
        tokens_module.tokens, ["ledger", "--session", session_id, "--task", task_id]
    )
    neither = runner.invoke(tokens_module.tokens, ["ledger"])
    assert both.exit_code == 2, both.output
    assert neither.exit_code == 2, neither.output


def test_ledger_command_keeps_unknown_spend_distinct(
    cli_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    runner: CliRunner,
) -> None:
    project_id = sample_project["id"]
    claude = _session(session_manager, project_id, "no-cost-state")
    grok_zero = _session(session_manager, project_id, "grok-zero", "grok")
    grok_incomplete = _session(session_manager, project_id, "grok-incomplete", "grok")
    _event(cli_db, project_id, claude, at=BASE, message_id="c-1")
    _event(cli_db, project_id, grok_zero, at=BASE, message_id="g-1", source="grok")
    _event(cli_db, project_id, grok_incomplete, at=BASE, message_id="g-2", source="grok")
    reported = ReportedUsageStore(cli_db)
    reported.upsert_runs(
        grok_zero,
        [_run("turn-1", source="grok", started=BASE, observed=BASE, amount="0")],
    )
    reported.upsert_runs(
        grok_incomplete,
        [_run("turn-1", source="grok", started=BASE, observed=BASE, complete=False)],
    )

    claude_out = runner.invoke(tokens_module.tokens, ["ledger", "--session", claude])
    assert claude_out.exit_code == 0, claude_out.output
    assert any(
        line.startswith("spend claude:") and "unknown (no reported run)" in line
        for line in claude_out.output.splitlines()
    ), claude_out.output

    zero_out = runner.invoke(tokens_module.tokens, ["ledger", "--session", grok_zero])
    assert zero_out.exit_code == 0, zero_out.output
    assert any(
        line.startswith("spend grok:") and "0 usd over 1 run" in line
        for line in zero_out.output.splitlines()
    ), zero_out.output

    incomplete_out = runner.invoke(tokens_module.tokens, ["ledger", "--session", grok_incomplete])
    assert incomplete_out.exit_code == 0, incomplete_out.output
    assert any(
        line.startswith("spend grok:")
        and "1 incomplete run" in line
        and "unknown" not in line
        and "usd" not in line
        for line in incomplete_out.output.splitlines()
    ), incomplete_out.output
