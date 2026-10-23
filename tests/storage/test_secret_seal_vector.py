"""Python opens the Rust-sealed token from the committed cross-language vector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import POSTURE_KEY_FILE, SecretStore

pytestmark = pytest.mark.integration

VECTOR_PATH = (
    Path(__file__).resolve().parents[2]
    / "crates"
    / "gruntime"
    / "tests"
    / "fixtures"
    / "secret_seal_vector.json"
)


def test_python_opens_rust_sealed_vector(temp_db: HubDatabase, tmp_path: Path) -> None:
    vector = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))
    kek_file = tmp_path / ".secret_kek"
    kek_file.write_text(vector["kek"], encoding="utf-8")
    kek_file.chmod(0o600)
    temp_db.execute(
        """INSERT INTO secret_key_material (id, wrapped_dek, kek_posture)
           VALUES ('default', %s, %s)""",
        (vector["wrapped_dek"], POSTURE_KEY_FILE),
    )
    store = SecretStore(temp_db, gobby_home=tmp_path)

    opened = store.open_sealed(vector["rust_token"], aad=vector["rust_aad"].encode("utf-8"))

    assert opened == vector["rust_plaintext"].encode("utf-8")
