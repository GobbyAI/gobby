from __future__ import annotations

import pytest

from gobby.tasks.diff_manifest import DiffPagingError, parse_numstat


def test_parse_numstat_normal_entry() -> None:
    assert parse_numstat(b"3\t4\tsrc/example.py\0") == {b"src/example.py": (3, 4)}


def test_parse_numstat_rename_applies_counts_to_both_paths() -> None:
    assert parse_numstat(b"5\t2\t\0old.py\0new.py\0") == {
        b"old.py": (5, 2),
        b"new.py": (5, 2),
    }


def test_parse_numstat_binary_entry_uses_none_counts() -> None:
    assert parse_numstat(b"-\t-\timage.bin\0") == {b"image.bin": (None, None)}


@pytest.mark.parametrize(
    "payload",
    [
        b"1\t2\tmissing-terminator",
        b"1\tbad\tpath\0",
        b"1\t2\t\0old-only\0",
    ],
)
def test_parse_numstat_rejects_malformed_output(payload: bytes) -> None:
    with pytest.raises(DiffPagingError, match="malformed") as exc_info:
        parse_numstat(payload)

    assert exc_info.value.code == "git_failed"
