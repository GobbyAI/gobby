"""Evidence paths, immutable writes and streaming input scans."""

from pathlib import Path

import pytest
from provision_environment import refresh_manifest
from runtime_boundary import contained_file, write_once
from validate_environment import ALLOWED_EXAMPLE_ENV, _scan_secrets


def test_existing_evidence_is_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    write_once(path, "original", 0o600)
    write_once(path, "original", 0o600)
    with pytest.raises(AssertionError, match="refusing to overwrite"):
        write_once(path, "replacement", 0o600)
    assert path.read_text() == "original"


def test_symlink_write_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("original")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(AssertionError, match="symlink output"):
        write_once(link, "replacement", 0o600)
    assert target.read_text() == "original"


def test_evidence_path_must_be_contained_and_regular(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("private")
    with pytest.raises(ValueError):
        contained_file(root, outside)
    (root / "link").symlink_to(outside)
    with pytest.raises(AssertionError, match="symlink in evidence"):
        contained_file(root, "link")


def test_manifest_refresh_rejects_unrecognized_case(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown comparator case"):
        refresh_manifest(tmp_path, "gcode", "../../outside")


def test_secret_scan_includes_large_files_and_chunk_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"x" * (3 * 1024 * 1024 - 10) + b"\nghp_" + b"A" * 30)
    with pytest.raises(AssertionError, match="github-token pattern"):
        _scan_secrets(tmp_path, {path.name})


def test_corpus_symlink_is_rejected(tmp_path: Path) -> None:
    for name in ALLOWED_EXAMPLE_ENV:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    link = tmp_path / "external"
    link.symlink_to(tmp_path / ".env.example")
    with pytest.raises(AssertionError, match="symlink in comparator input"):
        _scan_secrets(tmp_path, {link.name, *ALLOWED_EXAMPLE_ENV})
