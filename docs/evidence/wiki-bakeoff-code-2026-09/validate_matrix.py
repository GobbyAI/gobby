"""Validate the code-wiki bakeoff matrix and its controlled fixtures."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

BASE_SHA = "0216f1e33f05962d49467d95fe84609041c6dba8"
CHANGE_SHA = "8b24ac26699aac8b24254a647aa70b208287b492"
MATRIX_PATH = Path(__file__).with_name("matrix.md")

C3_PATHS = (
    ("M", "config/replenishment.toml"),
    ("M", "docs/replenishment.md"),
    ("M", "src/game_goblins/platform/settings.py"),
    ("M", "src/game_goblins/replenishment/daily.py"),
    ("M", "src/game_goblins/replenishment/planner.py"),
    ("M", "src/game_goblins/replenishment/store_targets.py"),
    ("M", "tests/platform/test_settings.py"),
    ("M", "tests/replenishment/test_planning_store.py"),
    ("A", "tests/replenishment/test_store_targets.py"),
)

C4_PATH = "src/game_goblins/replenishment/product_types.py"
C5_COUNTS = {
    C4_PATH: 3,
    "src/game_goblins/replenishment/set_sell_through.py": 4,
    "src/game_goblins/replenishment/vendor_workbook/queries.py": 2,
    "tests/replenishment/test_product_types.py": 3,
}
FIXTURE_PATHS = {
    "C4": (C4_PATH,),
    "C5": tuple(C5_COUNTS),
    "C6": (C4_PATH,),
}


def _git(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )
    return completed.stdout


def _git_text(repo: Path, commit: str, path: str) -> str:
    return _git(repo, "show", f"{commit}:{path}").decode("utf-8")


def _replace_exact(text: str, old: str, new: str, count: int, label: str) -> str:
    actual = text.count(old)
    if actual != count:
        raise AssertionError(f"{label}: expected {count} anchors, found {actual}")
    if new and new in text:
        raise AssertionError(f"{label}: replacement already exists in baseline")
    return text.replace(old, new)


def _c4(files: dict[str, str]) -> dict[str, str]:
    old = '"""Classify a catalog name using its final colon-delimited segment."""'
    new = (
        '"""Classify from the final colon-delimited segment so set names do not mask '
        'product form."""'
    )
    files[C4_PATH] = _replace_exact(files[C4_PATH], old, new, 1, "C4 docstring")
    return files


def _c5(files: dict[str, str]) -> dict[str, str]:
    old = "derive_demand_type"
    new = "classify_demand_type"
    for path, count in C5_COUNTS.items():
        files[path] = _replace_exact(files[path], old, new, count, f"C5 {path}")
    if sum(C5_COUNTS.values()) != 12:
        raise AssertionError("C5 replacement total is not 12")
    return files


def _c6(files: dict[str, str]) -> dict[str, str]:
    old_call = "            units = _ceil(rate.daily_rate * Decimal(horizon))"
    new_call = (
        "            units = int(\n"
        "                (rate.daily_rate * Decimal(horizon)).to_integral_value(\n"
        "                    rounding=ROUND_CEILING\n"
        "                )\n"
        "            )"
    )
    old_helper = (
        "\n\ndef _ceil(value: Decimal) -> int:\n"
        "    return int(value.to_integral_value(rounding=ROUND_CEILING))\n"
    )
    text = _replace_exact(files[C4_PATH], old_call, new_call, 1, "C6 call")
    files[C4_PATH] = _replace_exact(text, old_helper, "", 1, "C6 helper")
    return files


TRANSFORMS: dict[str, Callable[[dict[str, str]], dict[str, str]]] = {
    "C4": _c4,
    "C5": _c5,
    "C6": _c6,
}


def _expected_fixture(source_repo: Path, fixture: str) -> dict[str, str]:
    files = {path: _git_text(source_repo, BASE_SHA, path) for path in FIXTURE_PATHS[fixture]}
    transformed = TRANSFORMS[fixture](files)
    for path, text in transformed.items():
        compile(text, path, "exec")
    return transformed


def _case_sections(matrix: str) -> dict[str, str]:
    matches = list(re.finditer(r"(?m)^### (C[0-9]) —", matrix))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(matrix)
        sections[match.group(1)] = matrix[match.start() : end]
    return sections


def _validate_cases(matrix: str) -> None:
    sections = _case_sections(matrix)
    if set(sections) != {f"C{number}" for number in range(10)}:
        raise AssertionError("matrix must define exactly C0-C9")
    for case_id, section in sections.items():
        for field in (
            "**Inputs.**",
            "**Execute.**",
            "**Expected observations and metrics.**",
            "**Pass/fail.**",
        ):
            if field not in section:
                raise AssertionError(f"{case_id} is missing {field}")
    for case_id in ("C4", "C5", "C6"):
        section = sections[case_id]
        if "apply_patch" not in section:
            raise AssertionError(f"{case_id} must use apply_patch")
        for forbidden in ("compileall", "path.write_text"):
            if forbidden in section:
                raise AssertionError(f"{case_id} contains forbidden fixture recipe {forbidden}")
    for case_id in ("C8", "C9"):
        section = sections[case_id]
        for commit in (BASE_SHA, CHANGE_SHA):
            if commit not in section:
                raise AssertionError(f"{case_id} is missing commit binding {commit}")
        if "Baseline" not in section and "baseline" not in section:
            raise AssertionError(f"{case_id} must state the baseline Q14 non-failure rule")


def _table_rows(matrix: str, prefix: str) -> list[list[str]]:
    rows = []
    for line in matrix.splitlines():
        if line.startswith(f"| {prefix}"):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def _validate_question_bindings(matrix: str) -> None:
    domain_rows = _table_rows(matrix, "D")
    domains = {row[0] for row in domain_rows if re.fullmatch(r"D\d{2}", row[0])}
    if domains != {f"D{number:02d}" for number in range(1, 11)}:
        raise AssertionError("domain inventory must define D01-D10")
    for row in domain_rows:
        if re.fullmatch(r"D\d{2}", row[0]):
            expected = CHANGE_SHA if row[0] == "D10" else BASE_SHA
            if expected not in row[3]:
                raise AssertionError(f"{row[0]} citations are not pinned to {expected}")

    question_rows = _table_rows(matrix, "Q")
    questions = {row[0]: row for row in question_rows if re.fullmatch(r"Q\d{2}", row[0])}
    if set(questions) != {f"Q{number:02d}" for number in range(1, 15)}:
        raise AssertionError("question key must define Q01-Q14 exactly once")
    for question_id, row in questions.items():
        if len(row) != 6:
            raise AssertionError(f"{question_id} must have six table fields")
        expected = CHANGE_SHA if question_id == "Q14" else BASE_SHA
        if row[4] != f"`{expected}`":
            raise AssertionError(f"{question_id} must bind to {expected}")
        bindings = set(re.findall(r"D\d{2}", row[5]))
        if not bindings or not bindings <= domains:
            raise AssertionError(f"{question_id} has an invalid domain binding")


def _validate_controls(matrix: str) -> None:
    required = (
        "at most two independent hosted generator runs concurrently",
        "compatibility/probe 15 minutes",
        "hosted generation 60 minutes",
        "local generation 120 minutes",
        "one diagnosed whole-run retry",
        "explicit user continuation decision",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "qwen/qwen3.8-27b",
        "gpt-5.6-sol",
        "text-embedding-nomic-embed-text-v1.5@f16",
        "exact `redacted_prompts`",
        "prompt_artifact_paths",
        "subscription_quota_before",
        "question_source_commit",
        "not verified as byte-identical to that commit",
    )
    lowered = matrix.lower()
    for value in required:
        if value.lower() not in lowered:
            raise AssertionError(f"missing required control: {value}")
    for value in (
        "soft observation at 15 minutes",
        "diagnostic capture at 60 minutes",
        "hard stop at 120 minutes",
        "--max-concurrency 2",
    ):
        if value in matrix:
            raise AssertionError(f"obsolete control remains: {value}")
    for comparator in (
        "CodeWiki",
        "OpenDeepWiki",
        "Grok Wiki",
        "Understand Anything",
        "Archify",
    ):
        if f"| {comparator} |" not in matrix:
            raise AssertionError(f"missing P3 profile: {comparator}")
    for ask_id in ("P3-A1", "P3-A2", "P3-A3"):
        if matrix.count(f"| {ask_id} |") != 1:
            raise AssertionError(f"missing or duplicate exact Ask prompt: {ask_id}")


def _validate_c3(source_repo: Path, matrix: str) -> None:
    output = _git(source_repo, "diff", "--name-status", BASE_SHA, CHANGE_SHA).decode()
    actual = tuple(tuple(line.split("\t", 1)) for line in output.splitlines() if line)
    if actual != C3_PATHS:
        raise AssertionError(f"C3 path key differs from frozen Git objects: {actual!r}")
    for status, path in C3_PATHS:
        if f"{status} {path}" not in matrix:
            raise AssertionError(f"matrix C3 key is missing {status} {path}")


def validate_matrix(source_repo: Path) -> None:
    matrix = MATRIX_PATH.read_text(encoding="utf-8")
    _validate_cases(matrix)
    _validate_question_bindings(matrix)
    _validate_controls(matrix)
    _validate_c3(source_repo, matrix)
    for fixture in TRANSFORMS:
        _expected_fixture(source_repo, fixture)


def _tree_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def validate_fixture(
    source_repo: Path,
    fixture: str,
    baseline_root: Path,
    case_root: Path,
) -> None:
    expected = _expected_fixture(source_repo, fixture)
    baseline = _tree_files(baseline_root)
    case = _tree_files(case_root)
    changed = {
        path for path in baseline.keys() | case.keys() if baseline.get(path) != case.get(path)
    }
    intended = set(FIXTURE_PATHS[fixture])
    if changed != intended:
        raise AssertionError(
            f"{fixture} changed paths differ: expected {sorted(intended)}, got {sorted(changed)}"
        )
    for path in intended:
        frozen = _git_text(source_repo, BASE_SHA, path).encode()
        if baseline.get(path) != frozen:
            raise AssertionError(f"baseline template differs from frozen object: {path}")
        actual = case.get(path)
        if actual != expected[path].encode():
            raise AssertionError(f"{fixture} content differs from exact recipe: {path}")
        compile(expected[path], path, "exec")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-repo",
        type=Path,
        default=Path("/Users/josh/Projects/game-goblins"),
    )
    parser.add_argument("--fixture", choices=sorted(TRANSFORMS))
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--case-root", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        validate_matrix(args.source_repo)
        if args.fixture:
            if args.baseline_root is None or args.case_root is None:
                raise AssertionError("--fixture requires --baseline-root and --case-root")
            validate_fixture(
                args.source_repo,
                args.fixture,
                args.baseline_root,
                args.case_root,
            )
    except (AssertionError, OSError, subprocess.CalledProcessError, UnicodeError) as error:
        print(f"matrix validation failed: {error}", file=sys.stderr)
        return 1
    suffix = f" and {args.fixture} fixture" if args.fixture else ""
    print(f"matrix validation passed{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
