from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gobby.test_types._mypy import (
    MypyInvocationError,
    normalize_reported_path,
    parse_mypy_output,
    run_mypy,
)


def test_parse_mypy_output_maps_error_variants_to_namespaced_diagnostics(
    tmp_path: Path,
) -> None:
    absolute = tmp_path / "tests" / "test_absolute.py"
    output = "\n".join(
        [
            "tests/test_basic.py:3: error: Basic failure [arg-type]",
            "tests/test_columns.py:4:8: error: Column failure [assignment]",
            "tests/test_end.py:5:2:5:9: error: End column failure [return-value]",
            "tests/colon:name.py:6: error: Colon path failure",
            f"{absolute}:7: error: Absolute failure [call-arg]",
            "tests/test_unknown.py:8: error: Plain path failure",
            "tests/test_basic.py:3: note: A note",
            "Found 6 errors in 6 files (checked 6 source files)",
        ]
    )

    diagnostics = parse_mypy_output(output, root=tmp_path)

    assert [(item.path, item.line, item.code) for item in diagnostics] == [
        ("tests/test_basic.py", 3, "mypy:arg-type"),
        ("tests/test_columns.py", 4, "mypy:assignment"),
        ("tests/test_end.py", 5, "mypy:return-value"),
        ("tests/colon:name.py", 6, "mypy:unknown"),
        ("tests/test_absolute.py", 7, "mypy:call-arg"),
        ("tests/test_unknown.py", 8, "mypy:unknown"),
    ]
    assert diagnostics[0].message == "Basic failure"


def test_normalize_reported_path_converts_windows_separators_on_posix(tmp_path: Path) -> None:
    normalized = normalize_reported_path(r"tests\watchdog\test_reader.py", root=tmp_path)

    assert normalized == "tests/watchdog/test_reader.py"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path behavior")
def test_normalize_reported_path_rejects_absolute_windows_path_on_posix(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="outside the project root"):
        normalize_reported_path(r"C:\project\tests\test_reader.py", root=tmp_path)


def test_run_mypy_rejects_exit_one_without_parseable_errors(tmp_path: Path) -> None:
    checker = tmp_path / "checker.py"
    checker.write_text(
        "import sys\nprint('unrecognized checker output')\nsys.exit(1)\n",
        encoding="utf-8",
    )

    with pytest.raises(MypyInvocationError) as exc_info:
        run_mypy(
            ("tests",),
            root=tmp_path,
            mypy_command=f"{sys.executable} {checker}",
        )

    assert "exit code 1 produced no parseable errors" in str(exc_info.value)
    assert "unrecognized checker output" in str(exc_info.value)
