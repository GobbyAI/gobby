"""Contracts for Python's identity-enforcing gdaemon adapter."""

from __future__ import annotations

import json
import logging
import subprocess
from unittest.mock import Mock

import pytest

from gobby.storage import schema_contract
from gobby.storage.hub import postgres


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
