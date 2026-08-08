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


def test_repeated_prefixes_are_aliased_without_dropping_paths() -> None:
    prefix = "src/gobby/install/shared/skills/impeccable/scripts/"
    paths = [f"{prefix}live/generated-file-{index:03d}.mjs" for index in range(107)]
    evidence = build_close_diff_evidence(
        "".join(_added_file_diff(path) for path in paths),
        criteria="The complete released scripts inventory is present.",
    )

    assert evidence.manifest_count == 107
    assert evidence.manifest_chars <= 5_500
    assert "Path aliases (exact prefixes):" in evidence.text
    alias_line = next(
        line for line in evidence.text.splitlines() if line.startswith("- @") and " = " in line
    )
    alias, rendered_prefix = alias_line[2:].split(" = ", maxsplit=1)
    assert rendered_prefix.startswith(prefix)
    for path in paths:
        rendered_path = alias + path.removeprefix(rendered_prefix)
        assert f"- {rendered_path} (+1/-0)" in evidence.text


def test_manifest_still_fails_when_exact_paths_cannot_fit() -> None:
    paths = [f"unique-file-{index:03d}-{'x' * 80}.txt" for index in range(100)]

    with pytest.raises(ValidationEvidenceTooLarge, match="complete changed-file manifest"):
        build_close_diff_evidence(
            "".join(_added_file_diff(path) for path in paths),
            criteria="Every unique file is present.",
        )
