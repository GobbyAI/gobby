"""Offline safety and identity checks for pinned dependency archives."""

import io
import json
import tarfile
from pathlib import Path

import pytest
from extract_sources import ORIGINS, extract_source
from validate_environment import PINS


def archive_fixture(root: Path, member_name: str, *, symlink: bool = False) -> Path:
    sources = root / "sources"
    sources.mkdir()
    archive = sources / ORIGINS["graphify"][1]
    with tarfile.open(archive, "w:gz") as stream:
        member = tarfile.TarInfo(member_name)
        member.mode = 0o644
        if symlink:
            member.type = tarfile.SYMTYPE
            member.linkname = "/outside"
            stream.addfile(member)
        else:
            member.size = 8
            stream.addfile(member, io.BytesIO(b"evidence"))
    return archive


def test_extract_records_actual_archive_and_source_identity(tmp_path: Path) -> None:
    archive_fixture(tmp_path, f"graphify-{PINS['graphify']}/README.md")
    extract_source(tmp_path, "graphify")
    assert (tmp_path / "sources/graphify/README.md").read_bytes() == b"evidence"
    receipt = json.loads((tmp_path / "receipts/graphify-source.json").read_text())
    assert receipt["required_pin"] == PINS["graphify"]
    assert receipt["file_count"] == 1
    assert receipt["source_url"].endswith(PINS["graphify"])
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        extract_source(tmp_path, "graphify")


@pytest.mark.parametrize(
    "member_name,symlink",
    [
        ("/outside", False),
        (f"graphify-{PINS['graphify']}/../escape", False),
        ("graphify-wrong/README.md", False),
        (f"graphify-{PINS['graphify']}/link", True),
        (f"graphify-{PINS['graphify']}/.git/config", False),
    ],
)
def test_extract_rejects_unsafe_members_before_writing(
    tmp_path: Path, member_name: str, symlink: bool
) -> None:
    archive_fixture(tmp_path, member_name, symlink=symlink)
    with pytest.raises(ValueError, match="unsafe or unexpected"):
        extract_source(tmp_path, "graphify")
    assert not (tmp_path / "sources/graphify").exists()
