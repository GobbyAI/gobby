"""Contracts for Python's identity-enforcing gdaemon adapter."""

from __future__ import annotations

import ast
import importlib.resources
import json
import logging
import re
import subprocess
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gobby.storage import schema_contract
from gobby.storage.hub import postgres
from gobby.storage.schema_identity_pin import SchemaIdentityError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRODUCTION_PYTHON_ROOT = _REPO_ROOT / "src" / "gobby"
_POSTGRES_DDL = re.compile(
    r"\b(?P<verb>CREATE|ALTER|DROP)\s+"
    r"(?P<temporary>TEMP(?:ORARY)?\s+)?"
    r"(?P<object>TABLE|INDEX|SCHEMA|TYPE|FUNCTION|TRIGGER|SEQUENCE|CONSTRAINT|EXTENSION|VIEW)\b"
    r"|(?P<reindex>\AREINDEX)\s+(?:INDEX|TABLE|DATABASE|SYSTEM)\b",
    re.IGNORECASE,
)

# These SQL-adjacent cases are intentionally outside persistent PostgreSQL
# runtime schema authority: temporary staging, repair, FalkorDB Cypher, and the
# one-shot PostgreSQL installer.
# Container initdb and hub-backup SQL are not production Python and therefore
# are outside this scan.
_KEPT_ADJACENT_SQL = Counter(
    {
        ("src/gobby/cli/installers/postgres.py", "CREATE EXTENSION"): 1,
        ("src/gobby/code_index/_storage/files.py", "CREATE TEMP TABLE"): 1,
        ("src/gobby/code_index/_storage/files.py", "DROP TABLE"): 1,
        ("src/gobby/code_index/bm25_health.py", "REINDEX"): 1,
        ("src/gobby/memory/falkor_client.py", "CREATE INDEX"): 1,
    }
)


def _production_sql_ddl() -> Counter[tuple[str, str]]:
    found: Counter[tuple[str, str]] = Counter()
    for path in sorted(_PRODUCTION_PYTHON_ROOT.rglob("*.py")):
        relative_path = path.relative_to(_REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            for match in _POSTGRES_DDL.finditer(node.value):
                if match.group("reindex"):
                    operation = "REINDEX"
                else:
                    temporary = "TEMP " if match.group("temporary") else ""
                    operation = f"{match.group('verb')} {temporary}{match.group('object')}".upper()
                found[(relative_path, operation)] += 1
    return found


def test_production_python_has_no_persistent_postgres_ddl() -> None:
    assert _production_sql_ddl() == _KEPT_ADJACENT_SQL


def test_baseline_seals_four_column_interactive_principal() -> None:
    baseline = (_REPO_ROOT / "crates/gcore/assets/schema/baseline.sql").read_text()
    assert "DROP FUNCTION IF EXISTS gobby_agent_auth.issue_or_reuse_interactive_principal(" in (
        baseline
    )
    start = baseline.index(
        "CREATE OR REPLACE FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal("
    )
    returns = baseline[start : start + 800]
    assert "managed_execution_id UUID" in returns
    assert "reused BOOLEAN" in returns


def test_sweep_pins_database_in_child_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    monkeypatch.setattr(schema_contract, "resolve_native_bin", lambda name: "/managed/gdaemon")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("GOBBY_DATABASE_URL", "postgresql://decoy.example/decoy")

    schema_contract.sweep_test_schemas(
        "postgresql://gobby:secret@database.example/gobby",
        age_hours=2,
    )

    args = run.call_args.args[0]
    kwargs = run.call_args.kwargs
    assert args == [
        "/managed/gdaemon",
        "schema",
        "sweep-test-schemas",
        "--age-hours",
        "2",
    ]
    assert all("postgresql://" not in arg for arg in args)
    assert kwargs["env"]["GOBBY_DATABASE_URL"] == (
        "postgresql://gobby:secret@database.example/gobby"
    )


def test_apply_pins_database_and_expected_identity_in_child_environment(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="ready\n", stderr=""))
    monkeypatch.setattr(schema_contract, "resolve_native_bin", lambda name: "/managed/gdaemon")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("GOBBY_DATABASE_URL", "postgresql://decoy.example/decoy")
    caplog.set_level(logging.INFO, logger="gobby.storage.schema_contract")

    schema_contract.apply_schema(
        "postgresql://gobby:secret@database.example/gobby",
        schema="worker_schema",
    )

    args = run.call_args.args[0]
    kwargs = run.call_args.kwargs
    assert args == [
        "/managed/gdaemon",
        "schema",
        "apply",
        "--schema",
        "worker_schema",
    ]
    assert all("postgresql://" not in arg for arg in args)
    assert kwargs["env"]["GOBBY_DATABASE_URL"] == (
        "postgresql://gobby:secret@database.example/gobby"
    )
    assert json.loads(kwargs["env"]["GOBBY_EXPECTED_SCHEMA_IDENTITY"]) == (
        schema_contract.expected_schema_identity()
    )
    assert "gdaemon schema apply completed for schema worker_schema" in caplog.text


def test_apply_uses_connection_current_schema_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="ready\n", stderr=""))
    monkeypatch.setattr(schema_contract, "resolve_native_bin", lambda name: "/managed/gdaemon")
    monkeypatch.setattr(subprocess, "run", run)

    schema_contract.apply_schema("postgresql://gobby:secret@database.example/gobby")

    assert run.call_args.args[0] == ["/managed/gdaemon", "schema", "apply"]


def test_verify_pins_database_and_expected_identity_in_child_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="verified\n", stderr=""))
    monkeypatch.setattr(schema_contract, "resolve_native_bin", lambda name: "/managed/gdaemon")
    monkeypatch.setattr(subprocess, "run", run)

    schema_contract.verify_schema("postgresql://gobby:secret@database.example/gobby")

    assert run.call_args.args[0] == ["/managed/gdaemon", "schema", "verify"]
    assert run.call_args.kwargs["env"]["GOBBY_DATABASE_URL"] == (
        "postgresql://gobby:secret@database.example/gobby"
    )
    assert json.loads(run.call_args.kwargs["env"]["GOBBY_EXPECTED_SCHEMA_IDENTITY"]) == (
        schema_contract.expected_schema_identity()
    )


def test_apply_reports_actionable_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schema_contract, "resolve_native_bin", lambda name: None)

    with pytest.raises(schema_contract.SchemaContractError, match="gdaemon.*gobby install"):
        schema_contract.apply_schema("postgresql://gobby:secret@database.example/gobby")


def test_postgres_database_delegates_exact_conninfo_to_gdaemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply = Mock()
    monkeypatch.setattr(schema_contract, "apply_schema", apply)
    database = object.__new__(postgres.PostgresHubDatabase)
    database._conninfo = "postgresql://object.example/gobby?options=-csearch_path%3Dworker"

    database.apply_migrations()
    database.apply_destructive_migrations()

    assert apply.call_args_list == [
        ((database._conninfo,), {}),
        ((database._conninfo,), {"destructive": True}),
    ]


def test_apply_reports_failed_identity_handshake_without_leaking_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql://gobby:secret@database.example/gobby"
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [],
            2,
            stdout="",
            stderr="schema identity mismatch: expected runner protocol 1, observed 0\n",
        )
    )
    monkeypatch.setattr(schema_contract, "resolve_native_bin", lambda name: "/managed/gdaemon")
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(
        schema_contract.SchemaContractError, match="schema identity mismatch"
    ) as exc:
        schema_contract.apply_schema(database_url)

    assert database_url not in str(exc.value)


def test_apply_reports_timeout_as_actionable_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schema_contract, "resolve_native_bin", lambda name: "/managed/gdaemon")

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("gdaemon", 300)

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(schema_contract.SchemaContractError, match="timed out after 300 seconds"):
        schema_contract.apply_schema("postgresql://gobby:secret@database.example/gobby")


def test_expected_identity_reports_packaged_contract_violations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The packaged pin is validated by the shared identity contract, not a local copy."""
    packaged = {**schema_contract.expected_schema_identity(), "extra": 1}
    resource = SimpleNamespace(read_text=lambda: json.dumps(packaged))
    monkeypatch.setattr(
        importlib.resources,
        "files",
        lambda package: SimpleNamespace(joinpath=lambda name: resource),
    )

    with pytest.raises(
        schema_contract.SchemaContractError,
        match=r"Packaged schema_expected_identity\.json is invalid: .*must contain exactly",
    ) as exc:
        schema_contract.expected_schema_identity()

    assert isinstance(exc.value.__cause__, SchemaIdentityError)
