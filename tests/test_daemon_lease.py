"""Schema protections for native authority and encrypted credentials."""

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BASELINE_SQL = _REPO_ROOT / "crates/gcore/assets/schema/baseline.sql"


@pytest.mark.unit
def test_baseline_seals_deployment_runtime_and_interactive_ciphertext() -> None:
    baseline = _BASELINE_SQL.read_text(encoding="utf-8").lower()
    assert "create table deployment_runtime" in baseline
    assert "fencing_epoch bigint default 0 not null" in baseline
    assert "grant_signing_secret text not null" in baseline
    table_declaration = "create table gobby_agent_auth.interactive_credential_material"
    assert table_declaration in baseline
    assert "issue_or_reuse_interactive_principal" in baseline
    table_start = baseline.index(table_declaration)
    table_end = baseline.index(");", table_start) + 2
    material = baseline[table_start:table_end]
    assert "ciphertext text not null" in material
    assert "aad_identity text not null" in material
    for forbidden in ("dsn", "password", "connection_uri", "plaintext"):
        assert forbidden not in material
