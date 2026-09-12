from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.ask.test_validation import _valid_case

pytestmark = pytest.mark.unit


def test_only_reviewed_claims_are_published(tmp_path: Path) -> None:
    from gobby.ask.artifacts import AskArtifactStore
    from gobby.ask.claims import QuestionPart, ReviewClaimVerdict
    from gobby.ask.publication import PublicationError, publish_answer
    from gobby.ask.validation import validate_claims, validate_review

    draft, evidence, blobs, review = _valid_case()
    draft = draft.model_copy(
        update={
            "question_parts": draft.question_parts
            + (QuestionPart(id="limits", text="Known limitations"),)
        }
    )
    review = review.model_copy(
        update={
            "draft_hash": draft.content_hash,
            "claim_verdicts": (
                review.claim_verdicts[0],
                ReviewClaimVerdict(
                    claim_id="claim-stable",
                    accepted=False,
                    classification_supported=False,
                    support_diagnostics=("Inference exceeds the cited source.",),
                    rationale="Reject the unsupported inference.",
                ),
            ),
            "missing_question_parts": ("limits",),
        }
    )
    deterministic = validate_claims(draft, evidence, pinned_blobs=blobs)
    reviewed = validate_review(draft, evidence, deterministic, review)
    store = AskArtifactStore(tmp_path, "project", "run-1")

    wrong_store = AskArtifactStore(tmp_path, "project", "run-2")
    with pytest.raises(PublicationError, match="run identity"):
        publish_answer(
            wrong_store,
            draft,
            evidence,
            deterministic,
            reviewed,
            request={"question": draft.question},
            binding=evidence.snapshot_binding.model_dump(mode="json"),
            profiles={"investigator": "profile-a", "reviewer": "profile-b"},
            tool_identities=("gobby-code@0.5.0",),
            attempt_history=({"attempt": 1, "status": "reviewed"},),
        )

    with pytest.raises(PublicationError, match="mandatory review"):
        publish_answer(
            store,
            draft,
            evidence,
            deterministic,
            None,
            request={"question": draft.question},
            binding=evidence.snapshot_binding.model_dump(mode="json"),
            profiles={"investigator": "profile-a", "reviewer": "profile-b"},
            tool_identities=("gobby-code@0.5.0",),
            attempt_history=({"attempt": 1, "status": "review_missing"},),
        )
    assert not (store.run_root / "publication").exists()

    published = publish_answer(
        store,
        draft,
        evidence,
        deterministic,
        reviewed,
        request={"question": draft.question},
        binding=evidence.snapshot_binding.model_dump(mode="json"),
        profiles={"investigator": "profile-a", "reviewer": "profile-b"},
        tool_identities=("gobby-code@0.5.0",),
        attempt_history=({"attempt": 1, "status": "reviewed"},),
    )
    assert published.outcome == "partial"
    assert published.claim_ids == ("claim-return",)
    answer = published.answer
    assert [claim["id"] for claim in answer["claims"]] == [
        "claim-return",
        "unknown-limits",
    ]
    assert "claim-stable" not in published.markdown
    assert "Unknown: question part 2 remains unresolved." in published.markdown
    assert "Known limitations" not in published.markdown


def test_expiry_during_publication_never_exposes_an_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.ask import publication as publication_module
    from gobby.ask.artifacts import AskArtifactStore
    from gobby.ask.publication import publish_answer
    from gobby.ask.validation import validate_claims, validate_review

    draft, evidence, blobs, review = _valid_case()
    deterministic = validate_claims(draft, evidence, pinned_blobs=blobs)
    reviewed = validate_review(draft, evidence, deterministic, review)
    store = AskArtifactStore(tmp_path, "project", draft.run_id)
    crossed_deadline = False
    original_write = publication_module._write_file

    def stalled_write(path: Path, payload: bytes) -> None:
        nonlocal crossed_deadline
        original_write(path, payload)
        crossed_deadline = True

    def check_deadline() -> None:
        if crossed_deadline:
            raise TimeoutError("Ask deadline exceeded while publishing")

    monkeypatch.setattr(publication_module, "_write_file", stalled_write)

    with pytest.raises(TimeoutError, match="deadline exceeded"):
        publish_answer(
            store,
            draft,
            evidence,
            deterministic,
            reviewed,
            request={"question": draft.question},
            binding=evidence.snapshot_binding.model_dump(mode="json"),
            profiles={"investigator": "profile-a", "reviewer": "profile-b"},
            tool_identities=("gobby-code@0.5.0",),
            attempt_history=({"attempt": 1, "status": "reviewed"},),
            deadline_check=check_deadline,
        )

    assert not (store.run_root / "publication").exists()
    assert not tuple(store.run_root.glob(".publication-*.tmp"))


def test_accepted_unknowns_do_not_count_as_supported_question_coverage(
    tmp_path: Path,
) -> None:
    from gobby.ask.artifacts import AskArtifactStore
    from gobby.ask.claims import (
        AnswerSection,
        Claim,
        ClaimClassification,
        QuestionPart,
        ReviewClaimVerdict,
    )
    from gobby.ask.publication import publish_answer
    from gobby.ask.validation import validate_claims, validate_review

    draft, evidence, blobs, review = _valid_case()
    unknown = Claim(
        id="claim-limits",
        classification=ClaimClassification.UNKNOWN,
        statement="Recorded evidence does not determine deployment limits.",
        question_part_ids=("limits",),
        rationale="The recorded source only establishes the function's return behavior.",
    )
    draft = draft.model_copy(
        update={
            "question_parts": draft.question_parts
            + (QuestionPart(id="limits", text="Production is universally safe."),),
            "claims": draft.claims + (unknown,),
            "sections": (
                AnswerSection(
                    id="answer",
                    title="Production is universally safe.",
                    claim_ids=("claim-return", "claim-stable", unknown.id),
                ),
            ),
        }
    )
    review = review.model_copy(
        update={
            "draft_hash": draft.content_hash,
            "claim_verdicts": review.claim_verdicts
            + (
                ReviewClaimVerdict(
                    claim_id=unknown.id,
                    accepted=True,
                    classification_supported=True,
                    rationale="The stated unknown honestly reflects the evidence limit.",
                ),
            ),
        }
    )
    deterministic = validate_claims(draft, evidence, pinned_blobs=blobs)
    reviewed = validate_review(draft, evidence, deterministic, review)
    published = publish_answer(
        AskArtifactStore(tmp_path, "project", "run-1"),
        draft,
        evidence,
        deterministic,
        reviewed,
        request={"question": draft.question},
        binding=evidence.snapshot_binding.model_dump(mode="json"),
        profiles={"investigator": "profile-a", "reviewer": "profile-b"},
        tool_identities=("gobby-code@0.5.0",),
        attempt_history=({"attempt": 1, "status": "reviewed"},),
    )

    assert published.outcome == "partial"
    assert published.claim_ids == ("claim-return", "claim-stable", unknown.id)
    assert unknown.statement in published.markdown
    assert "Production is universally safe." not in published.markdown
    assert published.answer["sections"][0]["title"] == "Reviewed claims"

    unknown_only_review = review.model_copy(
        update={
            "claim_verdicts": tuple(
                verdict.model_copy(
                    update={
                        "accepted": verdict.claim_id == unknown.id,
                        "classification_supported": verdict.claim_id == unknown.id,
                        "support_diagnostics": (),
                    }
                )
                for verdict in review.claim_verdicts
            ),
            "missing_question_parts": ("behavior",),
        }
    )
    unknown_only = validate_review(draft, evidence, deterministic, unknown_only_review)
    unknown_publication = publish_answer(
        AskArtifactStore(tmp_path / "unknown", "project", "run-1"),
        draft,
        evidence,
        deterministic,
        unknown_only,
        request={"question": draft.question},
        binding=evidence.snapshot_binding.model_dump(mode="json"),
        profiles={"investigator": "profile-a", "reviewer": "profile-b"},
        tool_identities=("gobby-code@0.5.0",),
        attempt_history=({"attempt": 1, "status": "reviewed"},),
    )
    assert unknown_publication.outcome == "unknown"
    assert "Production is universally safe." not in unknown_publication.markdown


def test_atomic_publication_is_idempotent_after_crash_and_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.ask.artifacts import AskArtifactStore
    from gobby.ask.publication import PublicationError, publish_answer
    from gobby.ask.validation import validate_claims, validate_review

    draft, evidence, blobs, review = _valid_case()
    deterministic = validate_claims(draft, evidence, pinned_blobs=blobs)
    reviewed = validate_review(draft, evidence, deterministic, review)
    store = AskArtifactStore(tmp_path, "project", "run-1")
    arguments = {
        "request": {"question": draft.question},
        "binding": evidence.snapshot_binding.model_dump(mode="json"),
        "profiles": {"investigator": "profile-a", "reviewer": "profile-b"},
        "tool_identities": ("gobby-code@0.5.0",),
        "attempt_history": ({"attempt": 1, "status": "reviewed"},),
    }
    real_rename = os.rename

    def crash_before_publish(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        raise OSError("injected publication crash")

    monkeypatch.setattr("gobby.ask.publication.os.rename", crash_before_publish)
    with pytest.raises(OSError, match="injected publication crash"):
        publish_answer(
            store,
            draft,
            evidence,
            deterministic,
            reviewed,
            **arguments,
        )
    assert not (store.run_root / "publication").exists()

    monkeypatch.setattr("gobby.ask.publication.os.rename", real_rename)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _index: publish_answer(
                    store,
                    draft,
                    evidence,
                    deterministic,
                    reviewed,
                    **arguments,
                ),
                range(16),
            )
        )

    assert all(result.manifest_sha256 == results[0].manifest_sha256 for result in results)
    assert all(result.markdown == results[0].markdown for result in results)
    assert not list(store.run_root.glob(".publication-*.tmp"))

    forged_results = tuple(
        result.model_copy(update={"accepted": result.claim_id == "claim-stable"})
        for result in reviewed.results
    )
    forged_review = reviewed.model_copy(update={"results": forged_results})
    other_store = AskArtifactStore(tmp_path / "forged", "project", "run-1")
    with pytest.raises(PublicationError, match="premise closure"):
        publish_answer(
            other_store,
            draft,
            evidence,
            deterministic,
            forged_review,
            **arguments,
        )
