"""The installed gdaemon and gcode must be the archived checkout's schema head and version."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from runtime_boundary import EvidenceError
from schema_head import (
    IDENTITY_KEYS,
    check_schema_head,
    read_archived_gcode_version,
    read_archived_identity,
    schema_head_proof,
)

from gobby.utils.native_bin import native_bin_name

ARCHIVED = {
    "assets_root_hash": "f6d4c594",
    "baseline_checksum": "e5b08e67",
    "baseline_version": 420,
    "latest_checksum": "fe7e5507",
    "latest_version": 489,
    "runner_protocol": 1,
}


def _stage_archive(
    root: Path, identity: dict[str, Any] | None = None, gcode_version: str = "1.9.105"
) -> None:
    identity_path = root / "sources/gobby/src/gobby/storage/schema_expected_identity.json"
    identity_path.parent.mkdir(parents=True)
    identity_path.write_text(json.dumps(identity if identity is not None else ARCHIVED))
    crate = root / "sources/gobby/crates/gcode"
    crate.mkdir(parents=True)
    (crate / "Cargo.toml").write_text(
        f'[package]\nname = "gobby-code"\nversion = "{gcode_version}"\n'
    )


def test_archived_identity_is_read_for_exactly_the_six_identity_keys(tmp_path: Path) -> None:
    _stage_archive(tmp_path, identity={**ARCHIVED, "unrelated": "dropped"})

    assert read_archived_identity(tmp_path) == ARCHIVED
    assert set(IDENTITY_KEYS) == set(ARCHIVED)


def test_archived_identity_refuses_a_missing_key(tmp_path: Path) -> None:
    incomplete = {key: value for key, value in ARCHIVED.items() if key != "latest_version"}
    _stage_archive(tmp_path, identity=incomplete)

    with pytest.raises(EvidenceError, match="latest_version"):
        read_archived_identity(tmp_path)


def test_archived_gcode_version_is_the_crate_package_version(tmp_path: Path) -> None:
    _stage_archive(tmp_path, gcode_version="1.9.105")

    assert read_archived_gcode_version(tmp_path) == "1.9.105"


def test_archived_gcode_crate_without_a_package_version_is_refused(tmp_path: Path) -> None:
    _stage_archive(tmp_path, gcode_version="1.9.105")
    (tmp_path / "sources/gobby/crates/gcode/Cargo.toml").write_text('[package]\nname = "gcode"\n')

    with pytest.raises(EvidenceError, match="lacks package.version"):
        read_archived_gcode_version(tmp_path)


def test_matching_gdaemon_identity_and_gcode_version_pass_and_return_the_archived_identity(
    tmp_path: Path,
) -> None:
    _stage_archive(tmp_path)

    result = check_schema_head(
        tmp_path, gdaemon_identity=dict(ARCHIVED), gcode_version="gcode 1.9.105"
    )

    assert result == ARCHIVED


@pytest.mark.parametrize("key", list(ARCHIVED))
def test_any_schema_identity_difference_stops_the_leaf(tmp_path: Path, key: str) -> None:
    _stage_archive(tmp_path)
    installed = {**ARCHIVED, key: "cutover-drift"}

    with pytest.raises(EvidenceError, match="schema head mismatch"):
        check_schema_head(tmp_path, gdaemon_identity=installed, gcode_version="gcode 1.9.105")


def test_a_gdaemon_identity_missing_a_key_is_a_mismatch(tmp_path: Path) -> None:
    _stage_archive(tmp_path)
    installed = {key: value for key, value in ARCHIVED.items() if key != "runner_protocol"}

    with pytest.raises(EvidenceError, match="schema head mismatch"):
        check_schema_head(tmp_path, gdaemon_identity=installed, gcode_version="gcode 1.9.105")


def test_installed_gcode_must_carry_the_crate_version(tmp_path: Path) -> None:
    _stage_archive(tmp_path, gcode_version="1.9.105")

    with pytest.raises(EvidenceError, match="is not the crates/gcode version"):
        check_schema_head(tmp_path, gdaemon_identity=dict(ARCHIVED), gcode_version="gcode 1.9.1040")


def _stage_binary(root: Path, name: str, output: str) -> None:
    bin_dir = root / "gobby-home/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / native_bin_name(name)
    binary.write_text(f"#!/bin/sh\nprintf '%s' '{output}'\n")
    binary.chmod(0o755)


def test_schema_head_proof_reads_the_installed_set_and_passes_when_it_is_the_archived_head(
    tmp_path: Path,
) -> None:
    _stage_archive(tmp_path)
    _stage_binary(tmp_path, "gdaemon", json.dumps(ARCHIVED))
    _stage_binary(tmp_path, "gcode", "gcode 1.9.105\n")

    proof = schema_head_proof(tmp_path)

    assert proof == {"identity": ARCHIVED, "gcode_version": "gcode 1.9.105"}


def test_schema_head_proof_stops_on_a_drifted_installed_gdaemon(tmp_path: Path) -> None:
    _stage_archive(tmp_path)
    _stage_binary(tmp_path, "gdaemon", json.dumps({**ARCHIVED, "latest_version": 488}))
    _stage_binary(tmp_path, "gcode", "gcode 1.9.105\n")

    with pytest.raises(EvidenceError, match="schema head mismatch"):
        schema_head_proof(tmp_path)


def test_a_gdaemon_payload_that_is_not_json_is_refused_with_bounded_output(tmp_path: Path) -> None:
    _stage_archive(tmp_path)
    _stage_binary(tmp_path, "gdaemon", "x" * 500)
    _stage_binary(tmp_path, "gcode", "gcode 1.9.105\n")

    with pytest.raises(EvidenceError, match="malformed JSON") as error:
        schema_head_proof(tmp_path)

    assert "x" * 201 not in str(error.value)


def test_a_gdaemon_payload_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    _stage_archive(tmp_path)
    _stage_binary(tmp_path, "gdaemon", json.dumps([ARCHIVED["latest_version"]]))
    _stage_binary(tmp_path, "gcode", "gcode 1.9.105\n")

    with pytest.raises(EvidenceError, match="non-object payload"):
        schema_head_proof(tmp_path)
