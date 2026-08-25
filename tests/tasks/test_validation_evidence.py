from __future__ import annotations

import pytest

from gobby.tasks.validation_evidence import (
    build_close_diff_evidence,
    extract_criteria_anchors,
)

pytestmark = pytest.mark.unit


def _added_file_diff(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        "+content\n"
    )


def _large_file_diff(path: str, lines: int, *, planted: dict[int, str] | None = None) -> str:
    body = [
        f"diff --git a/{path} b/{path}",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -0,0 +{lines} @@",
    ]
    for index in range(lines):
        if planted and index in planted:
            body.append(planted[index])
        else:
            body.append(f"+filler line {index}")
    return "\n".join(body) + "\n"


def test_complete_manifest_and_textual_diffs_are_preserved() -> None:
    prefix = "src/gobby/install/shared/skills/impeccable/scripts/"
    paths = [f"{prefix}live/generated-file-{index:03d}.mjs" for index in range(107)]
    evidence = build_close_diff_evidence(
        "".join(_added_file_diff(path) for path in paths),
        criteria="The complete released scripts inventory is present.",
    )

    assert evidence.manifest_count == 107
    assert evidence.truncated is False
    assert evidence.dropped_chars == 0
    for path in paths:
        assert f"- {path} (+1/-0 LOC)" in evidence.text
        assert f"diff --git a/{path} b/{path}" in evidence.text


def test_evidence_within_budget_is_complete() -> None:
    diff = _added_file_diff("src/small.py")
    evidence = build_close_diff_evidence(
        diff,
        criteria="The change is present.",
        budget_chars=10_000,
    )

    assert evidence.truncated is False
    assert "Complete textual diff:" in evidence.text
    assert "+content" in evidence.text


def test_budgeted_evidence_fits_with_every_file_represented() -> None:
    paths = [f"src/pkg/module_{index:02d}.py" for index in range(40)]
    diff = "".join(_large_file_diff(path, 200) for path in paths)
    assert len(diff) > 60_000
    budget = 20_000

    evidence = build_close_diff_evidence(
        diff,
        criteria="Every touched module keeps its behavior and the docs stay accurate.",
        budget_chars=budget,
    )

    assert evidence.truncated is True
    assert evidence.dropped_chars > 0
    assert len(evidence.text) <= budget
    assert "NOTE: diff evidence was truncated" in evidence.text
    for path in paths:
        assert f"- {path} (+200/-0 LOC)" in evidence.text
        assert f"diff --git a/{path} b/{path}" in evidence.text
        assert f"from {path} to fit the close-review budget" in evidence.text


def test_waterfill_keeps_small_files_complete_while_large_files_absorb_the_cut() -> None:
    small_paths = [f"src/small_{index}.py" for index in range(5)]
    diff = "".join(_added_file_diff(path) for path in small_paths)
    diff += _large_file_diff("src/huge.py", 3_000)

    evidence = build_close_diff_evidence(
        diff,
        criteria="The small helpers and the large module both change coherently.",
        budget_chars=12_000,
    )

    assert evidence.truncated is True
    for path in small_paths:
        assert f"diff --git a/{path} b/{path}" in evidence.text
        # Small files fit their fair share and keep their complete diff.
        assert f"from {path} to fit" not in evidence.text
    assert "from src/huge.py to fit the close-review budget" in evidence.text
    assert "+filler line 2999" not in evidence.text


def test_criteria_named_strings_survive_truncation() -> None:
    planted = {
        2_400: "+    run('uv run pytest tests/tasks/test_validation.py -v')",
        2_500: "+    port = 60892",
        2_600: "+    update('src/gobby/tasks/validation_evidence.py')",
    }
    diff = _large_file_diff("src/huge.py", 3_000, planted=planted)
    criteria = (
        "1. `uv run pytest tests/tasks/test_validation.py -v` passes.\n"
        "2. The isolated daemon binds port 60892.\n"
        "3. src/gobby/tasks/validation_evidence.py truncates per file."
    )

    evidence = build_close_diff_evidence(diff, criteria=criteria, budget_chars=8_000)

    assert evidence.truncated is True
    # Would fail if truncation removed the criteria-named strings: they sit far
    # past the head excerpt, between filler lines that are dropped.
    assert "uv run pytest tests/tasks/test_validation.py -v" in evidence.text
    assert "60892" in evidence.text
    assert "src/gobby/tasks/validation_evidence.py" in evidence.text
    assert "criteria-referenced lines retained" in evidence.text
    assert "+filler line 2401" not in evidence.text


def test_anchor_extraction_finds_commands_paths_identifiers_and_numbers() -> None:
    anchors = extract_criteria_anchors(
        "1. `uv run ruff check src/` is clean.\n"
        "2. Cold review wall-clock drops from 84 s and the cap stays 256,000 chars.\n"
        "3. close_task links the commit; see docs/contracts/plan-coverage.md."
    )
    texts = {anchor.text for anchor in anchors}

    assert "uv run ruff check src/" in texts
    assert "close_task" in texts
    assert "docs/contracts/plan-coverage.md" in texts
    assert "84" in texts
    assert "256000" in texts


def test_number_anchors_match_across_digit_group_separators() -> None:
    diff = _large_file_diff("src/config.py", 2_000, planted={1_500: "+CAP = 256_000"})

    evidence = build_close_diff_evidence(
        diff,
        criteria="The cap stays 256,000 characters.",
        budget_chars=6_000,
    )

    assert evidence.truncated is True
    assert "+CAP = 256_000" in evidence.text


def test_binary_payload_is_omitted_while_file_statistics_remain() -> None:
    diff = """diff --git a/image.bin b/image.bin
new file mode 100644
index 0000000..1234567
GIT binary patch
literal 4
LcmeAS@N?(olHy`u
"""

    evidence = build_close_diff_evidence(diff, criteria="Binary image is present.")

    assert "image.bin (+0/-0 LOC, binary payload omitted)" in evidence.text
    assert "[binary payload omitted; file statistics retained]" in evidence.text
    assert "LcmeAS@N?(olHy`u" not in evidence.text
