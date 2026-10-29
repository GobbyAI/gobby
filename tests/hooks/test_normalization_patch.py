"""Patch inspection flags must be distinguished from filenames and option values."""

import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.normalization import normalize_tool_fields

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("arguments", "kind"),
    [
        ("--dry-run original change.patch", "execute"),
        ("--check original change.patch", "execute"),
        ("-C original change.patch", "execute"),
        ("-fCu original change.patch", "execute"),
        ("-p0 --dry-run original change.patch", "execute"),
        ("--output=out --dry-run original change.patch", "execute"),
        ("--dry-run --output out original change.patch", "execute"),
        ("-o --dry-run original change.patch", "write"),
        ("-r --dry-run original change.patch", "write"),
        ("-i --dry-run original", "write"),
        ("-d --dry-run original change.patch", "write"),
        ("-z --dry-run original change.patch", "write"),
        ("-B --dry-run original change.patch", "write"),
        ("--output --dry-run original change.patch", "write"),
        ("--suffix --dry-run original change.patch", "write"),
        ("-- --dry-run change.patch", "write"),
        ("original --dry-run", "write"),
        ("-b --dry-run original change.patch", "write"),
        ("--output=--dry-run original change.patch", "write"),
        ("-fo--dry-run original change.patch", "write"),
        ("--dry-run --unknown original change.patch", "write"),
        ("--dry-run -o", "write"),
        ("--dry-run=$FLAG original change.patch", "write"),
    ],
)
def test_patch_inspection_uses_option_arity(tmp_path: Path, arguments: str, kind: str) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": f"patch {arguments}", "cwd": str(tmp_path)},
        "project_path": str(tmp_path),
    }

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == kind
    assert bool(data.get("canonical_repo_mutation")) is (kind == "write")


SINGLE_DIFF = "--- source\n+++ source\n@@ -1 +1 @@\n-before\n+after\n"


def _patch_invocation(tmp_path: Path, command: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(tmp_path)},
        "project_path": str(tmp_path),
    }
    normalize_tool_fields(data)
    return data


@pytest.mark.parametrize("output", ["", "-o results/patched "])
@pytest.mark.parametrize("backup", ["-z.orig", "--suffix=.custom -V numbered"])
def test_single_patch_scopes_original_output_and_sidecars(
    tmp_path: Path, output: str, backup: str
) -> None:
    (tmp_path / "change.patch").write_text(SINGLE_DIFF, encoding="utf-8")
    data = _patch_invocation(
        tmp_path,
        f"patch -g0 {backup} {output}-r rejects/failed source/original change.patch",
    )

    assert data["canonical_tool_kind"] == "write"
    assert not data.get("canonical_repo_mutation_scope_unknown")
    expected = {str(tmp_path / "source"), str(tmp_path / "rejects")}
    if output:
        expected.add(str(tmp_path / "results"))
    assert expected <= set(data["canonical_write_file_paths"])


def test_patch_directory_rebases_input_and_destination(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "change.patch").write_text(SINGLE_DIFF, encoding="utf-8")

    data = _patch_invocation(tmp_path, "patch -d work -g0 -z.orig source/original change.patch")

    assert not data.get("canonical_repo_mutation_scope_unknown")
    assert str(work / "source") in data["canonical_write_file_paths"]


@pytest.mark.parametrize(
    "arguments",
    [
        "source/original change.patch",
        "-g0 source/original change.patch",
        "-z.orig source/original change.patch",
        "-g1 -z.orig source/original change.patch",
        "-g0 -z/../../escape source/original change.patch",
        "-g0 -z.orig -B elsewhere/ source/original change.patch",
        "-g0 -z.orig -o output -i change.patch",
        "-g0 -z.orig source/original missing.patch",
        "-g0 -z.orig source/original -",
    ],
)
def test_patch_without_complete_scope_evidence_remains_unknown(
    tmp_path: Path, arguments: str
) -> None:
    (tmp_path / "change.patch").write_text(SINGLE_DIFF, encoding="utf-8")

    data = _patch_invocation(tmp_path, f"patch {arguments}")

    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "patch_text",
    [
        SINGLE_DIFF * 2,
        SINGLE_DIFF + "1a\nextra\n.\n",
        SINGLE_DIFF.replace("@@ -1 +1", "@@ -1,2 +1"),
        SINGLE_DIFF.replace("@@ -1 +1", "@@ -1," + "9" * 5000 + " +1"),
        "",
    ],
)
def test_patch_rejects_multiple_or_incomplete_diffs(tmp_path: Path, patch_text: str) -> None:
    (tmp_path / "change.patch").write_text(patch_text, encoding="utf-8")

    data = _patch_invocation(tmp_path, "patch -g0 -z.orig source/original change.patch")

    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "command",
    [
        "cp multi.patch change.patch; patch -g0 -z.orig source/original change.patch",
        "patch -g0 -z.orig source/original change.patch >change.patch",
        "patch -g0 -z.orig source/original change.patch | tee change.patch",
    ],
)
def test_patch_input_rewrites_invalidate_scope(tmp_path: Path, command: str) -> None:
    (tmp_path / "change.patch").write_text(SINGLE_DIFF, encoding="utf-8")

    data = _patch_invocation(tmp_path, command)

    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("matches", [False, True])
@pytest.mark.parametrize("backup_mode", ["simple", "numbered"])
def test_real_patch_side_effects_stay_inside_proved_directories(
    tmp_path: Path, matches: bool, backup_mode: str
) -> None:
    executable = shutil.which("patch")
    if executable is None:
        pytest.skip("patch executable is unavailable")
    destination = tmp_path / "source"
    destination.mkdir()
    original = destination / "original"
    original.write_text("before\n" if matches else "different\n", encoding="utf-8")
    reject = destination / "original.rej"
    reject.write_text("existing rejection\n", encoding="utf-8")
    (tmp_path / "change.patch").write_text(SINGLE_DIFF, encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    args = [
        "-g0",
        "-z.custom",
        "-V",
        backup_mode,
        "-f",
        "-r",
        "source/original.rej",
        "source/original",
        "change.patch",
    ]
    data = _patch_invocation(tmp_path, shlex.join(["patch", *args]))

    result = subprocess.run([executable, *args], cwd=tmp_path, capture_output=True, check=False)

    assert result.returncode == (0 if matches else 1), result.stderr.decode()
    assert not data.get("canonical_repo_mutation_scope_unknown")
    if matches:
        assert original.read_text(encoding="utf-8") == "after\n"
    else:
        assert reject.read_text(encoding="utf-8") != "existing rejection\n"
    changed = {
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and before.get(path) != path.read_bytes()
    }
    assert changed
    assert all(
        any(path.is_relative_to(parent) for parent in data["canonical_write_file_paths"])
        for path in changed
    )
