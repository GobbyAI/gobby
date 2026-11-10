"""Handoff admissions describe five executables, never provider credentials."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.e2e.composer_proof import ProofRefused
from tests.e2e.handoff_continuation_admission import load_admission, verify_binary_set

pytestmark = pytest.mark.unit
_SHA = "a" * 40
_TREE = "b" * 40
_PROVIDERS = ("claude", "codex", "grok", "agy", "droid")


@pytest.fixture
def admission_file(tmp_path: Path) -> Path:
    executable = tmp_path / "provider"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    binary = {
        "path": str(executable),
        "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }
    payload = {
        "grant": "PD_AUTHORIZED_ISOLATED_EXECUTION",
        "reviewed_commit": _SHA,
        "reviewed_tree": _TREE,
        "issued_at": datetime.now(UTC).isoformat(),
        "exclusion_message_id": "00000000-0000-4000-8000-000000000001",
        "complete_exclusion_map": True,
        "excluded_identities": [f"00000000-0000-4000-8000-{n:012d}" for n in range(26)],
        "native": dict.fromkeys(("gcode", "gdaemon", "ghook", "gterm", "gclient"), binary),
        "providers": dict.fromkeys(_PROVIDERS, binary),
        "support_tools": [],
        "artifact_dir": str(tmp_path),
        "worktree_test_notice_id": "00000000-0000-4000-8000-000000000002",
    }
    target = tmp_path / "admission.json"
    target.write_text(json.dumps(payload))
    target.chmod(0o600)
    return target


def _git_value(checkout: Path, *arguments: str) -> str:
    values: dict[tuple[str, ...], str] = {
        ("rev-parse", "HEAD"): _SHA,
        ("rev-parse", "HEAD^{tree}"): _TREE,
    }
    return values.get(arguments, "")


def test_all_five_provider_executables_need_no_auth_references(admission_file: Path) -> None:
    with patch("tests.e2e.composer_proof_admission.git_read", side_effect=_git_value):
        spec = load_admission(admission_file, admission_file.parent)
    assert set(spec.providers) == set(_PROVIDERS)
    assert "auth_reference" not in spec.model_dump_json()


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_missing_provider_is_a_named_failure(admission_file: Path, provider: str) -> None:
    with patch("tests.e2e.composer_proof_admission.git_read", side_effect=_git_value):
        spec = load_admission(admission_file, admission_file.parent)
    # The missing provider check runs before unrelated native identity validation.
    spec.providers[provider] = spec.providers[provider].model_copy(
        update={"path": Path("/missing")}
    )
    with pytest.raises(ProofRefused, match=provider):
        verify_binary_set(spec)


def test_private_auth_fields_are_refused(admission_file: Path) -> None:
    data: dict[str, Any] = json.loads(admission_file.read_text())
    data["providers"]["claude"]["auth_reference"] = "/never/read/this"
    admission_file.write_text(json.dumps(data))
    with patch("tests.e2e.composer_proof_admission.git_read", side_effect=_git_value):
        with pytest.raises(ProofRefused, match="admission"):
            load_admission(admission_file, admission_file.parent)
