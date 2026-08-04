"""Tests for evidence-independent coordinator plan handoff."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest

from gobby.plans.handoff_manifest_service import PlanHandoffManifestService
from gobby.plans.parser import PlanParseError
from gobby.plans.review_evidence_io import atomic_write_bytes
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager

pytestmark = pytest.mark.unit

ROUTING: dict[str, object] = {
    "1.1": {
        "category": "code",
        "implementation_domain": "backend",
        "tdd": True,
    }
}


@pytest.fixture
def handoff_setup(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> tuple[PlanHandoffManifestService, str, Path]:
    project = LocalProjectManager(temp_db).create(
        name="plan-handoff",
        repo_path=str(tmp_path),
    )
    plan_dir = tmp_path / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "handoff.md"
    plan_path.write_text(
        "\n".join(
            [
                "# Handoff",
                "**Plan ID:** handoff",
                "",
                "## P1 Phase",
                "`kind: framing`",
                "",
                "### 1.1 Implement handoff",
                "`kind: deliverable`",
                "",
                "Target: `src/example.py`",
                "",
                "**Acceptance:**",
                "- 1.1.1 — Handoff is implemented. test: `tests/test_example.py`",
                "",
                "## Task Mapping",
                "`kind: framing`",
                "",
                "One implementation leaf.",
                "",
                "## V1 Plan Changelog",
                "`kind: verification`",
                "",
                "Human handoff selected.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return PlanHandoffManifestService(temp_db), project.id, plan_path


def _apply(
    service: PlanHandoffManifestService,
    project_id: str,
    plan_path: Path,
    derived: dict[str, object],
) -> dict[str, object]:
    return service.apply(
        project_id=project_id,
        plan_path=plan_path,
        routing_decisions=ROUTING,
        source_plan_hash=cast(str, derived["source_plan_hash"]),
        rendered_plan_hash=cast(str, derived["rendered_plan_hash"]),
        manifest_digest=cast(str, derived["manifest_digest"]),
    )


def test_canonical_derivation_apply_and_idempotent_retry(
    handoff_setup: tuple[PlanHandoffManifestService, str, Path],
) -> None:
    service, project_id, plan_path = handoff_setup

    derived = service.derive(
        project_id=project_id,
        plan_path=plan_path,
        routing_decisions=ROUTING,
    )

    entries = cast(list[dict[str, object]], derived["manifest_entries"])
    assert derived["entry_count"] == 1
    assert entries[0]["source_section"] == "1.1"
    assert entries[0]["implementation_domain"] == "backend"
    assert entries[0]["tdd"] is True
    assert entries[0]["labels"] == ["covers:handoff:1.1:1.1.1"]

    applied = _apply(service, project_id, plan_path, derived)
    assert applied["applied"] is True
    assert hashlib.sha256(plan_path.read_bytes()).hexdigest() == derived["rendered_plan_hash"]

    retry = _apply(service, project_id, plan_path, derived)
    assert retry["applied"] is False
    assert retry["idempotent"] is True


def test_apply_rejects_plan_drift(
    handoff_setup: tuple[PlanHandoffManifestService, str, Path],
) -> None:
    service, project_id, plan_path = handoff_setup
    derived = service.derive(
        project_id=project_id,
        plan_path=plan_path,
        routing_decisions=ROUTING,
    )
    plan_path.write_text(plan_path.read_text().replace("One implementation", "A changed"))
    drifted = plan_path.read_bytes()

    with pytest.raises(ReviewEvidenceError) as exc_info:
        _apply(service, project_id, plan_path, derived)

    assert exc_info.value.code == "plan_drift"
    assert plan_path.read_bytes() == drifted


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("manifest_digest", "0" * 64, "manifest_digest_mismatch"),
        ("rendered_plan_hash", "0" * 64, "rendered_plan_hash_mismatch"),
    ],
)
def test_apply_rejects_digest_and_rendered_hash_mismatch(
    handoff_setup: tuple[PlanHandoffManifestService, str, Path],
    field: str,
    value: str,
    code: str,
) -> None:
    service, project_id, plan_path = handoff_setup
    derived = service.derive(
        project_id=project_id,
        plan_path=plan_path,
        routing_decisions=ROUTING,
    )
    original = plan_path.read_bytes()
    arguments = {
        "source_plan_hash": cast(str, derived["source_plan_hash"]),
        "rendered_plan_hash": cast(str, derived["rendered_plan_hash"]),
        "manifest_digest": cast(str, derived["manifest_digest"]),
    }
    arguments[field] = value

    with pytest.raises(ReviewEvidenceError) as exc_info:
        service.apply(
            project_id=project_id,
            plan_path=plan_path,
            routing_decisions=ROUTING,
            **arguments,
        )

    assert exc_info.value.code == code
    assert plan_path.read_bytes() == original


def test_apply_is_atomic_on_write_failure(
    handoff_setup: tuple[PlanHandoffManifestService, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project_id, plan_path = handoff_setup
    derived = service.derive(
        project_id=project_id,
        plan_path=plan_path,
        routing_decisions=ROUTING,
    )
    original = plan_path.read_bytes()

    def fail_write(_path: Path, _content: bytes) -> None:
        raise OSError("simulated atomic write failure")

    monkeypatch.setattr(
        "gobby.plans.handoff_manifest_service.atomic_write_bytes",
        fail_write,
    )
    with pytest.raises(OSError, match="simulated atomic write failure"):
        _apply(service, project_id, plan_path, derived)
    assert plan_path.read_bytes() == original
    monkeypatch.setattr(
        "gobby.plans.handoff_manifest_service.atomic_write_bytes",
        atomic_write_bytes,
    )


def test_path_escape_and_symlink_are_rejected(
    handoff_setup: tuple[PlanHandoffManifestService, str, Path],
) -> None:
    service, project_id, plan_path = handoff_setup

    with pytest.raises(ReviewEvidenceError) as escape:
        service.derive(
            project_id=project_id,
            plan_path="../outside.md",
            routing_decisions=ROUTING,
        )
    assert escape.value.code == "invalid_plan_path"

    symlink = plan_path.with_name("symlink.md")
    symlink.symlink_to(plan_path)
    with pytest.raises(ReviewEvidenceError) as linked:
        service.derive(
            project_id=project_id,
            plan_path=symlink,
            routing_decisions=ROUTING,
        )
    assert linked.value.code == "invalid_plan_path"


def test_expansion_parse_failure_prevents_derivation(
    handoff_setup: tuple[PlanHandoffManifestService, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project_id, plan_path = handoff_setup
    original = plan_path.read_bytes()

    def fail_expansion(_path: Path, _rendered: bytes, *, plan_id: str | None) -> None:
        _ = plan_id
        raise PlanParseError([(1, "forced expansion failure")], plan_path)

    monkeypatch.setattr(
        "gobby.plans.handoff_manifest_service._parse_expansion",
        fail_expansion,
    )
    with pytest.raises(ReviewEvidenceError) as exc_info:
        service.derive(
            project_id=project_id,
            plan_path=plan_path,
            routing_decisions=ROUTING,
        )
    assert exc_info.value.code == "invalid_plan"
    assert plan_path.read_bytes() == original
