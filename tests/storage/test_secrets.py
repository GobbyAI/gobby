"""Canonical SecretStore behavior after legacy migration removal."""

from pathlib import Path

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SECRET_MATERIAL_FILENAMES, SecretStore

pytestmark = pytest.mark.unit


def test_secret_round_trip_uses_only_canonical_key_material(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))

    SecretStore(temp_db).set("CANONICAL_SECRET", "encrypted-value")

    assert SecretStore(temp_db).get("CANONICAL_SECRET") == "encrypted-value"
    assert SECRET_MATERIAL_FILENAMES == (".secret_kek",)
    assert (tmp_path / ".secret_kek").is_file()
    assert not (tmp_path / ".secret_salt").exists()
