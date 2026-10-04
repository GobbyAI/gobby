"""Tests for API key storage and local-daemon key adoption."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from gobby.storage.api_keys import ApiKeyManager, ensure_local_api_key
from gobby.storage.auth import hash_token
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.utils import api_key_format
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

MACHINE_A = "8fa1247f-e924-4bd7-a54e-b9dd5704304a"
MACHINE_B = "54ba70ce-3ec4-470d-905a-dcb40704abfd"
ABSENT_KEY_ID = "6f1d2c3b-4a5e-4f60-8b71-9c2d3e4f5a6b"


@pytest.fixture
def db(hub_db: HubDatabase) -> HubDatabase:
    machines = LocalMachineManager(hub_db)
    machines.upsert_seen(MACHINE_A, TEST_USER_ID)
    machines.upsert_seen(MACHINE_B, TEST_USER_ID)
    return hub_db


def _bootstrap(tmp_path: Path, extra: dict[str, Any] | None = None) -> Path:
    files_home = tmp_path / "files"
    files_home.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "bootstrap.yaml"
    data: dict[str, Any] = {"datastore_mode": "local", "files_home": str(files_home)}
    data.update(extra or {})
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    path.chmod(0o600)
    return path


def _bootstrap_key(path: Path) -> tuple[str | None, str | None]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data.get("api_key"), data.get("api_key_id")


def _keys(db: HubDatabase, machine_id: str = MACHINE_A) -> list[dict[str, Any]]:
    rows = db.fetchall(
        "SELECT id::TEXT AS id, key_hash, revoked_at FROM api_keys "
        "WHERE machine_id = %s ORDER BY created_at",
        (machine_id,),
    )
    return [dict(row) for row in rows]


def _live_ids(db: HubDatabase) -> list[str]:
    return [row["id"] for row in _keys(db) if row["revoked_at"] is None]


def _fail_directory_fsync() -> AbstractContextManager[Any]:
    real_fsync = os.fsync
    calls: list[int] = []

    def _fsync(fd: int) -> None:
        calls.append(fd)
        if len(calls) == 2:
            raise OSError("directory fsync failed")
        real_fsync(fd)

    return patch("gobby.utils.durable_file.os.fsync", side_effect=_fsync)


class _FailReadbackAfterReplace:
    """Fail the first bootstrap read after ``os.replace``: durable_replace's readback."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.replaced = False
        self.failed = False
        self._real_replace = os.replace
        self._real_read_bytes = Path.read_bytes

    def _replace(self, src: Any, dst: Any) -> None:
        self._real_replace(src, dst)
        self.replaced = True

    def _read_bytes(self, target: Path) -> bytes:
        if target == self.path and self.replaced and not self.failed:
            self.failed = True
            raise OSError("readback failed")
        return self._real_read_bytes(target)

    def __enter__(self) -> None:
        self._patches = [
            patch("gobby.utils.durable_file.os.replace", side_effect=self._replace),
            patch.object(Path, "read_bytes", autospec=True, side_effect=self._read_bytes),
        ]
        for active in self._patches:
            active.start()

    def __exit__(self, *exc: object) -> None:
        for active in reversed(self._patches):
            active.stop()


def test_ensure_local_api_key_mints_once_and_follows_publication_point(
    db: HubDatabase, tmp_path: Path
) -> None:
    """4.2.4: one adoption per bootstrap; the rename decides revoke versus keep."""
    path = _bootstrap(tmp_path / "fresh")

    ensure_local_api_key(db, MACHINE_A, path)
    ensure_local_api_key(db, MACHINE_A, path)

    api_key, api_key_id = _bootstrap_key(path)
    assert api_key is not None and api_key_id is not None
    assert api_key_format.parse(api_key) is not None
    assert _live_ids(db) == [api_key_id]
    assert _keys(db)[0]["key_hash"] == hash_token(api_key)

    before_rename = _bootstrap(tmp_path / "before-rename")
    original = before_rename.read_bytes()
    with (
        patch("gobby.utils.durable_file.os.replace", side_effect=OSError("replace failed")),
        pytest.raises(OSError, match="replace failed"),
    ):
        ensure_local_api_key(db, MACHINE_A, before_rename)
    assert before_rename.read_bytes() == original
    assert _live_ids(db) == [api_key_id]
    assert len(_keys(db)) == 2

    injections: list[tuple[str, Callable[[Path], AbstractContextManager[Any]]]] = [
        ("directory fsync failed", lambda _path: _fail_directory_fsync()),
        ("readback failed", _FailReadbackAfterReplace),
    ]
    for message, inject in injections:
        committed = _bootstrap(tmp_path / message.replace(" ", "-"))
        live_before = set(_live_ids(db))
        with inject(committed), pytest.raises(OSError, match=message):
            ensure_local_api_key(db, MACHINE_A, committed)
        _, published_id = _bootstrap_key(committed)
        assert published_id is not None
        assert set(_live_ids(db)) == live_before | {published_id}

        count = len(_keys(db))
        ensure_local_api_key(db, MACHINE_A, committed)
        assert len(_keys(db)) == count
        assert _bootstrap_key(committed)[1] == published_id


def test_concurrent_adoption_mints_one_key(db: HubDatabase, tmp_path: Path) -> None:
    """4.2.12: the bootstrap lock serializes adopters onto one key."""
    path = _bootstrap(tmp_path)
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def _adopt() -> None:
        barrier.wait()
        try:
            ensure_local_api_key(db, MACHINE_A, path)
        except Exception as exc:  # surfaced through the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_adopt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    api_key, api_key_id = _bootstrap_key(path)
    assert api_key is not None
    rows = _keys(db)
    assert [row["id"] for row in rows] == [api_key_id]
    assert rows[0]["key_hash"] == hash_token(api_key)
    assert rows[0]["revoked_at"] is None


def test_legacy_config_path_skips_adoption(
    db: HubDatabase, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """4.2.13: a legacy config.yaml is never written and mints nothing."""
    legacy = tmp_path / "config.yaml"
    legacy.write_text("datastore_mode: local\n", encoding="utf-8")
    original = legacy.read_bytes()

    with caplog.at_level(logging.WARNING, logger="gobby.storage.api_keys"):
        ensure_local_api_key(db, MACHINE_A, legacy)

    assert legacy.read_bytes() == original
    assert not (tmp_path / "bootstrap.yaml").exists()
    assert _keys(db) == []
    assert "gobby install" in caplog.text
    assert "bootstrap.yaml" in caplog.text


def test_ensure_local_api_key_replaces_stale_bootstrap_key(db: HubDatabase, tmp_path: Path) -> None:
    """4.2.18: a live key is kept; any stale key is replaced and left unrevoked."""
    keys = ApiKeyManager(db)
    live_key, live = keys.mint(TEST_USER_ID, MACHINE_A, "local daemon")
    live_path = _bootstrap(tmp_path / "live", {"api_key": live_key, "api_key_id": live.id})
    ensure_local_api_key(db, MACHINE_A, live_path)
    assert _bootstrap_key(live_path) == (live_key, live.id)
    assert _live_ids(db) == [live.id]

    revoked_key, revoked = keys.mint(TEST_USER_ID, MACHINE_A, "revoked")
    assert keys.revoke(revoked.id, TEST_USER_ID)
    foreign_key, foreign = keys.mint(TEST_USER_ID, MACHINE_B, "other machine")
    stale = {
        "absent": (api_key_format.generate(), ABSENT_KEY_ID),
        "revoked": (revoked_key, revoked.id),
        "hash-mismatched": (api_key_format.generate(), live.id),
        "other-machine": (foreign_key, foreign.id),
    }
    for name, (stale_key, stale_id) in stale.items():
        path = _bootstrap(tmp_path / name, {"api_key": stale_key, "api_key_id": stale_id})
        revoked_before = {row["id"]: row["revoked_at"] for row in _keys(db) + _keys(db, MACHINE_B)}

        ensure_local_api_key(db, MACHINE_A, path)

        new_key, new_id = _bootstrap_key(path)
        assert new_id not in revoked_before, name
        assert new_key is not None and new_key != stale_key, name
        rows = {row["id"]: row for row in _keys(db) + _keys(db, MACHINE_B)}
        assert rows[new_id]["key_hash"] == hash_token(new_key), name
        assert rows[new_id]["revoked_at"] is None, name
        for key_id, revoked_at in revoked_before.items():
            assert rows[key_id]["revoked_at"] == revoked_at, name
