from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.ask.test_validation import _valid_case

pytestmark = pytest.mark.unit


def test_portable_byte_stable_export(tmp_path: Path) -> None:
    from gobby.ask.artifacts import AskArtifactStore
    from gobby.ask.export import ExportCollisionError, export_publication
    from gobby.ask.publication import PublicationError, publish_answer, replay_publication
    from gobby.ask.validation import validate_claims, validate_review

    draft, evidence, blobs, review = _valid_case()
    deterministic = validate_claims(draft, evidence, pinned_blobs=blobs)
    reviewed = validate_review(draft, evidence, deterministic, review)
    store = AskArtifactStore(tmp_path / "state", "project", "run-1")
    published = publish_answer(
        store,
        draft,
        evidence,
        deterministic,
        reviewed,
        request={"question": draft.question},
        binding=evidence.repository_binding.model_dump(mode="json"),
        profiles={"investigator": "profile-a", "reviewer": "profile-b"},
        tool_identities=("gobby-code@0.5.0",),
        attempt_history=({"attempt": 1, "status": "reviewed"},),
    )

    replay = replay_publication(published.root)
    assert replay.answer_json == (published.root / "answer.json").read_bytes()
    assert replay.answer_markdown == (published.root / "answer.md").read_bytes()

    destination = tmp_path / "portable-export"
    exported = export_publication(published.root, destination)
    source_files = sorted(
        path.relative_to(published.root) for path in published.root.rglob("*") if path.is_file()
    )
    assert source_files == sorted(
        path.relative_to(exported) for path in exported.rglob("*") if path.is_file()
    )
    for relative in source_files:
        assert (exported / relative).read_bytes() == (published.root / relative).read_bytes()

    markdown = (exported / "answer.md").read_text()
    assert "](evidence/" in markdown
    assert str(store.run_root) not in markdown
    excerpt_files = list((exported / "evidence").glob("*.txt"))
    assert len(excerpt_files) == 1
    assert excerpt_files[0].read_text() == "    return 1\n"

    with pytest.raises(ExportCollisionError):
        export_publication(published.root, destination)

    unmanifested = published.root / "evidence" / "manifest.json"
    unmanifested.write_text("not the bundle manifest")
    with pytest.raises(PublicationError, match="unmanifested"):
        replay_publication(published.root)
    unmanifested.unlink()

    (published.root / "answer.md").write_text("tampered")
    with pytest.raises(PublicationError, match="hash mismatch"):
        replay_publication(published.root)


def test_concurrent_export_never_overwrites_destination(tmp_path: Path) -> None:
    from gobby.ask.artifacts import AskArtifactStore
    from gobby.ask.export import ExportCollisionError, export_publication
    from gobby.ask.publication import publish_answer
    from gobby.ask.validation import validate_claims, validate_review

    draft, evidence, blobs, review = _valid_case()
    deterministic = validate_claims(draft, evidence, pinned_blobs=blobs)
    reviewed = validate_review(draft, evidence, deterministic, review)
    store = AskArtifactStore(tmp_path / "state", "project", "run-1")
    published = publish_answer(
        store,
        draft,
        evidence,
        deterministic,
        reviewed,
        request={"question": draft.question},
        binding=evidence.repository_binding.model_dump(mode="json"),
        profiles={"investigator": "profile-a", "reviewer": "profile-b"},
        tool_identities=("gobby-code@0.5.0",),
        attempt_history=({"attempt": 1, "status": "reviewed"},),
    )
    destination = tmp_path / "race-export"

    def export_once(_index: int) -> bool:
        try:
            export_publication(published.root, destination)
        except ExportCollisionError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(export_once, range(16)))

    assert outcomes.count(True) == 1
    assert (destination / "manifest.json").read_bytes() == (
        published.root / "manifest.json"
    ).read_bytes()


def test_export_crash_leaves_destination_absent_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.ask.artifacts import AskArtifactStore
    from gobby.ask.export import export_publication
    from gobby.ask.publication import publish_answer
    from gobby.ask.validation import validate_claims, validate_review

    draft, evidence, blobs, review = _valid_case()
    deterministic = validate_claims(draft, evidence, pinned_blobs=blobs)
    reviewed = validate_review(draft, evidence, deterministic, review)
    store = AskArtifactStore(tmp_path / "state", "project", "run-1")
    published = publish_answer(
        store,
        draft,
        evidence,
        deterministic,
        reviewed,
        request={"question": draft.question},
        binding=evidence.repository_binding.model_dump(mode="json"),
        profiles={"investigator": "profile-a", "reviewer": "profile-b"},
        tool_identities=("gobby-code@0.5.0",),
        attempt_history=({"attempt": 1, "status": "reviewed"},),
    )
    destination = tmp_path / "crash-export"
    real_rename = os.rename

    def crash(_source: object, _target: object) -> None:
        raise OSError("injected export crash")

    monkeypatch.setattr("gobby.ask.export.os.rename", crash)
    with pytest.raises(OSError, match="injected export crash"):
        export_publication(published.root, destination)
    assert not destination.exists()

    monkeypatch.setattr("gobby.ask.export.os.rename", real_rename)
    assert export_publication(published.root, destination) == destination
