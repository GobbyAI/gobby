from __future__ import annotations

from pathlib import Path
from typing import Never

import pytest

from gobby.plans.review_evidence_io import render_manifest_plan
from gobby.plans.review_evidence_models import ReviewEvidenceError

pytestmark = pytest.mark.unit

_ENTRY: dict[str, object] = {
    "title": "Implement example",
    "category": "code",
    "task_type": "feature",
    "depends_on": [],
    "validation_criteria": "1.1.1: Example behavior is tested.",
    "labels": ["covers:review-evidence:1.1:1.1.1"],
    "tdd": True,
    "source_section": "1.1",
    "implementation_domain": "backend",
}


def _plan_text(*, manifest_count: int = 0, trailing_section: bool = False) -> str:
    lines = [
        "# Review Evidence",
        "**Plan ID:** review-evidence",
        "",
        "## P1 Phase",
        "`kind: framing`",
        "",
        "### 1.1 Work",
        "`kind: deliverable`",
        "",
        "Target: `src/example.py`",
        "",
        "**Acceptance:**",
        "- 1.1.1 — Behavior exists. test: `tests/test_example.py`",
        "",
        "## Task Mapping",
        "`kind: framing`",
        "",
        "Pending.",
        "",
        "## V1 Plan Changelog",
        "`kind: verification`",
        "",
        "No rounds yet.",
    ]
    for _index in range(manifest_count):
        lines.extend(
            [
                "",
                "## M1 Task Manifest",
                "`kind: manifest`",
                "",
                "```yaml",
                "[]",
                "```",
            ]
        )
    if trailing_section:
        lines.extend(["", "## Appendix", "`kind: framing`", "", "Too late."])
    return "\n".join(lines) + "\n"


def test_render_manifest_rejects_duplicate_m1_sections(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    content = _plan_text(manifest_count=2).encode()
    plan_path.write_bytes(content)

    with pytest.raises(ReviewEvidenceError) as error:
        render_manifest_plan(plan_path, content, [_ENTRY])

    assert error.value.code == "duplicate_manifest_key"


def test_render_manifest_rejects_nonfinal_m1_section(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    content = _plan_text(manifest_count=1, trailing_section=True).encode()
    plan_path.write_bytes(content)

    with pytest.raises(ReviewEvidenceError) as error:
        render_manifest_plan(plan_path, content, [_ENTRY])

    assert error.value.code == "invalid_manifest"


def test_rendered_plan_validation_uses_temporary_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = tmp_path / "plan.md"
    content = _plan_text().encode()
    plan_path.write_bytes(content)

    def forbid_named_temporary_file(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("render validation must not create a sibling temporary file")

    monkeypatch.setattr(
        "gobby.plans.review_evidence_io.NamedTemporaryFile",
        forbid_named_temporary_file,
    )

    rendered = render_manifest_plan(plan_path, content, [_ENTRY])

    assert b"## M1 Task Manifest" in rendered
    assert plan_path.read_bytes() == content
