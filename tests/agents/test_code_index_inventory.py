from __future__ import annotations

from pathlib import Path

import pytest

from gobby.agents import code_index
from gobby.agents.code_index import (
    GcodeCommandError,
    IndexInventoryError,
    _preview_process_detail,
    _process_detail,
    repository_source_digest,
    settle_indexed_value,
)

pytestmark = pytest.mark.unit


def test_repository_digest_does_not_read_regular_file_bodies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "service.py"
    source.write_text("version = 1\n", encoding="utf-8")

    def fail_read_bytes(_path: Path) -> bytes:
        raise AssertionError("repository digest must use file metadata")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    digest = repository_source_digest(tmp_path, source_files=("service.py",))

    assert digest.source_files == ("service.py",)
    assert len(digest.digest) == 64


def test_process_detail_keeps_full_redacted_output() -> None:
    body = b"postgres://user:hunter2@localhost/db " + (b"x" * 600)
    detail = _process_detail(b"", body)

    assert "hunter2" not in detail
    assert "<redacted>" in detail
    assert "x" * 600 in detail
    preview = _preview_process_detail(detail)
    assert preview.startswith("[truncated]\n")
    assert len(preview) < len(detail)
    error = GcodeCommandError("gcode_index_failed:1:preview", output=detail)
    assert error.output == detail


def test_settle_reenumerates_unpinned_repository_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b\n", encoding="utf-8")
    inventories = iter(
        [
            ("a.py",),
            ("a.py", "b.py"),
            ("a.py", "b.py"),
            ("a.py", "b.py"),
        ]
    )
    monkeypatch.setattr(code_index, "_git_visible_source_files", lambda _root: next(inventories))

    result = settle_indexed_value(
        tmp_path,
        index_operation=lambda: None,
        read_last_indexed_at=lambda: "2026-07-28T00:00:00Z",
        derive=lambda: "settled",
        max_attempts=2,
        backoff_seconds=0,
    )

    assert result == "settled"


def test_settle_normalizes_supported_derive_failures(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")

    def fail_derive() -> str:
        raise OSError("derived inventory unavailable")

    with pytest.raises(IndexInventoryError) as exc_info:
        settle_indexed_value(
            tmp_path,
            index_operation=lambda: None,
            read_last_indexed_at=lambda: "2026-07-28T00:00:00Z",
            derive=fail_derive,
            source_files=("a.py",),
        )

    assert exc_info.value.code == "inventory_unavailable"
    assert "derivation failed" in str(exc_info.value)
