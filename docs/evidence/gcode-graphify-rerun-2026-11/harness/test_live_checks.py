"""Live-run checks: rerun corpora match September, and the isolated binary set applies cleanly."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import validate_environment as validation
from provision_environment import build_manifest
from runtime_boundary import EvidenceError, write_once

from gobby.storage import schema_divergence
from gobby.utils.native_bin import IDENTITY_STAMP_NAME, SET_MEMBERS, native_bin_name

CASES = {"gcode": ("C0",)}


def _manifest_text(case_root: Path) -> str:
    return json.dumps(build_manifest(case_root, "a" * 40, []), indent=2) + "\n"


def _write_corpus(root: Path, body: str) -> Path:
    case_root = root / "corpora/gcode/C0"
    write_once(case_root / "src/app.py", body, 0o644)
    write_once(root / "manifests/corpora/gcode/C0.json", _manifest_text(case_root), 0o644)
    return case_root


def _write_september(september: Path, manifest_text: str) -> None:
    write_once(september / "manifests/corpora/gcode/C0.json", manifest_text, 0o644)


def test_live_corpora_pass_when_manifests_match_and_files_are_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    september = tmp_path / "september"
    case_root = _write_corpus(root, "print('one')\n")
    _write_september(september, _manifest_text(case_root))

    report = validation.validate_corpora_live(root, september, cases=CASES)

    assert report == {"gcode/C0": {"missing": [], "changed": [], "extra": []}}


def test_live_corpora_fail_when_a_manifest_differs_from_september(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    september = tmp_path / "september"
    _write_corpus(root, "print('one')\n")
    case_root = root / "corpora/gcode/C0"
    _write_september(
        september, json.dumps(build_manifest(case_root, "b" * 40, []), indent=2) + "\n"
    )

    with pytest.raises(EvidenceError, match="manifest differs from September"):
        validation.validate_corpora_live(root, september, cases=CASES)


def test_live_corpora_fail_when_a_corpus_file_changed_after_its_manifest(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    september = tmp_path / "september"
    case_root = _write_corpus(root, "print('one')\n")
    _write_september(september, _manifest_text(case_root))
    (case_root / "src/app.py").write_text("print('two')\n")

    with pytest.raises(EvidenceError, match="corpora differ from their manifests"):
        validation.validate_corpora_live(root, september, cases=CASES)


def _set_member_names() -> list[str]:
    return [native_bin_name(member) for member in SET_MEMBERS]


def test_binary_set_proof_requires_every_member_and_the_identity_stamp(tmp_path: Path) -> None:
    (tmp_path / "gobby-home/bin").mkdir(parents=True)

    with pytest.raises(EvidenceError, match="isolated binary set is missing"):
        validation.binary_set_proof(tmp_path)


def _stage_binary_set(root: Path) -> None:
    bin_dir = root / "gobby-home/bin"
    bin_dir.mkdir(parents=True)
    for name in _set_member_names():
        (bin_dir / name).write_bytes(name.encode())
    (bin_dir / IDENTITY_STAMP_NAME).write_text("{}\n")


def test_binary_set_proof_records_member_and_stamp_hashes_when_the_set_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_binary_set(tmp_path)
    monkeypatch.setattr(schema_divergence, "binary_set_apply_refusal", lambda bin_dir=None: None)

    proof = validation.binary_set_proof(tmp_path)

    assert proof["refusal"] is None
    member = native_bin_name("gcode")
    assert proof["members"]["gcode"]["path"] == f"gobby-home/bin/{member}"
    assert proof["members"]["gcode"]["sha256"] == hashlib.sha256(member.encode()).hexdigest()
    assert proof["stamp"]["path"] == f"gobby-home/bin/{IDENTITY_STAMP_NAME}"
    assert proof["stamp"]["sha256"] == hashlib.sha256(b"{}\n").hexdigest()


def test_binary_set_proof_fails_when_the_installed_set_refuses_to_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_binary_set(tmp_path)
    monkeypatch.setattr(
        schema_divergence,
        "binary_set_apply_refusal",
        lambda bin_dir=None: "mixed installed binary set",
    )

    with pytest.raises(EvidenceError, match="binary set apply refused"):
        validation.binary_set_proof(tmp_path)
