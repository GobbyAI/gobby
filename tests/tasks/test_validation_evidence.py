from __future__ import annotations

import pytest

from gobby.tasks.validation_evidence import (
    ValidationEvidenceTooLarge,
    build_close_diff_evidence,
)


def _added_file_diff(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        "+content\n"
    )


def test_complete_manifest_and_textual_diffs_are_preserved() -> None:
    prefix = "src/gobby/install/shared/skills/impeccable/scripts/"
    paths = [f"{prefix}live/generated-file-{index:03d}.mjs" for index in range(107)]
    evidence = build_close_diff_evidence(
        "".join(_added_file_diff(path) for path in paths),
        criteria="The complete released scripts inventory is present.",
    )

    assert evidence.manifest_count == 107
    for path in paths:
        assert f"- {path} (+1/-0 LOC)" in evidence.text
        assert f"diff --git a/{path} b/{path}" in evidence.text


def test_explicit_evidence_bound_fails_without_truncating() -> None:
    paths = [f"unique-file-{index:03d}-{'x' * 80}.txt" for index in range(100)]

    with pytest.raises(ValidationEvidenceTooLarge, match="complete task-close diff evidence"):
        build_close_diff_evidence(
            "".join(_added_file_diff(path) for path in paths),
            criteria="Every unique file is present.",
            max_chars=5_500,
        )


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
