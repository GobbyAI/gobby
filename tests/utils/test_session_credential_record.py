"""Reads the owner-only session credential record that a CLI's SessionStart stored."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.utils.session_credential_record import (
    RECORD_NAMESPACE,
    STORE_DIR,
    cli_pid_key,
    load_record,
    record_path,
)

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-2222-4333-8444-555555555555"
TOKEN = "session-token-for-the-bound-session"


def _write(home: Path, key: str, body: str, mode: int = 0o600) -> Path:
    directory = home / STORE_DIR
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = record_path(home, key)
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


def _store(home: Path, key: str, payload: dict[str, str], mode: int = 0o600) -> Path:
    return _write(home, key, json.dumps(payload), mode)


def test_record_name_matches_the_rust_store_for_a_cli_pid() -> None:
    # gcore derives the same UUIDv5 from its RECORD_NAMESPACE; this is the shared golden name.
    assert RECORD_NAMESPACE.hex == "25855c0de00040008000000025855001"
    assert record_path(Path("/home"), cli_pid_key(4242)) == Path(
        "/home/session-credentials/d94d9bec-bb52-5978-b9f8-1c9d4c49d457.json"
    )


def test_load_record_returns_the_stored_credential(tmp_path: Path) -> None:
    _store(tmp_path, cli_pid_key(4242), {"token": TOKEN, "session_id": SESSION_ID})

    record = load_record(tmp_path, cli_pid_key(4242))

    assert record is not None
    assert record.token == TOKEN
    assert record.session_id == SESSION_ID


def test_load_record_is_absent_for_another_cli_pid(tmp_path: Path) -> None:
    _store(tmp_path, cli_pid_key(4242), {"token": TOKEN, "session_id": SESSION_ID})

    assert load_record(tmp_path, cli_pid_key(5555)) is None


def test_load_record_refuses_a_record_that_is_not_owner_only(tmp_path: Path) -> None:
    _store(tmp_path, cli_pid_key(4242), {"token": TOKEN, "session_id": SESSION_ID}, mode=0o644)

    assert load_record(tmp_path, cli_pid_key(4242)) is None


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"session_id": SESSION_ID}),
        json.dumps({"token": "", "session_id": SESSION_ID}),
        json.dumps({"token": TOKEN, "session_id": 7}),
        json.dumps(["not", "an", "object"]),
        "{not json",
    ],
)
def test_load_record_refuses_a_malformed_record(tmp_path: Path, body: str) -> None:
    _write(tmp_path, cli_pid_key(4242), body)

    assert load_record(tmp_path, cli_pid_key(4242)) is None


def test_record_repr_never_shows_the_token(tmp_path: Path) -> None:
    _store(tmp_path, cli_pid_key(4242), {"token": TOKEN, "session_id": SESSION_ID})

    record = load_record(tmp_path, cli_pid_key(4242))

    assert record is not None
    assert TOKEN not in repr(record)
