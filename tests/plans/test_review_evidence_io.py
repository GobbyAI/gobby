from __future__ import annotations

import json
from pathlib import Path
from typing import Never

import pytest

from gobby.plans.review_evidence_io import parse_checkpoints, render_manifest_plan
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


def _checkpoint_plan(round_result: str) -> bytes:
    return (
        "# Plan\n\n## V1 Plan Changelog\n`kind: verification`\n\n"
        "```json plan-review-round\n"
        '{"evidence_id":"e1","round_number":1,"plan_hash":"h1",'
        f'"round_result":{round_result},"session_id":"s1"}}\n'
        "```\n"
    ).encode()


def test_parse_checkpoints_accepts_round_result_without_convergence_telemetry() -> None:
    """Durable checkpoints predate later required round_result fields.

    `convergence_telemetry` became mandatory after these records were written.
    Re-validating history here would permanently block preparation of any plan
    that already carries rounds.
    """
    plan = _checkpoint_plan('{"verdict":"needs_review","findings":[]}')

    checkpoints = parse_checkpoints(plan)

    assert len(checkpoints) == 1
    assert checkpoints[0]["round_result"] == {"verdict": "needs_review", "findings": []}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence_id", '""'),
        ("round_number", "0"),
        ("plan_hash", '""'),
        ("session_id", '""'),
    ],
)
def test_parse_checkpoints_still_rejects_malformed_envelopes(field: str, value: str) -> None:
    payload: dict[str, object] = {
        "evidence_id": "e1",
        "round_number": 1,
        "plan_hash": "h1",
        "round_result": {"verdict": "needs_review", "findings": []},
        "session_id": "s1",
    }
    payload[field] = json.loads(value)
    plan = (
        "# Plan\n\n## V1 Plan Changelog\n`kind: verification`\n\n"
        "```json plan-review-round\n"
        f"{json.dumps(payload)}\n```\n"
    ).encode()

    with pytest.raises(ReviewEvidenceError) as error:
        parse_checkpoints(plan)

    assert error.value.code == "checkpoint_reconciliation_error"


def test_parse_checkpoints_rejects_non_object_round_result() -> None:
    plan = _checkpoint_plan('"not-an-object"')

    with pytest.raises(ReviewEvidenceError) as error:
        parse_checkpoints(plan)

    assert error.value.code == "checkpoint_reconciliation_error"
