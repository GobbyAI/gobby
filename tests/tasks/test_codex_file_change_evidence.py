"""Native Codex add receipts preserve RED-time test source for gate 12."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.config.validation_detection import ValidationDetectionConfig
from gobby.storage.session_models import Session
from gobby.tasks import transcript_evidence_cache
from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.tdd_compacted_output import has_compacted_pytest_failure
from gobby.tasks.tdd_python_evidence import _original_test_module
from gobby.tasks.transcript_evidence import _derive_transcript_path_evidence
from gobby.tasks.transcript_evidence_snapshots import load_durable_snapshot, store_durable_snapshot

pytestmark = pytest.mark.unit
START = datetime(2026, 10, 14, 21, tzinfo=UTC)
TEST_PATH = "tests/test_feature.py"
SOURCE = (
    "def test_feature():\n    assert feature() == 2\n\n"
    "def test_other():\n    assert feature() == 3\n"
)
RED = (
    ">       assert feature() == 2\nE       assert 1 == 2\n\n"
    ">       assert feature() == 3\nE       assert 1 == 3\n\n"
    "FAILED tests/test_feature.py::test_feature\n"
    "FAILED tests/test_feature.py::test_other\n"
    "================ 2 failed in 0.01s ================\n"
    "[full output: rtk recall abcdef123456]\n"
)


@pytest.mark.parametrize(
    ("status", "kind", "content", "record_type", "confirmed", "checkpoint"),
    [
        ("completed", "add", SOURCE, "event_msg", True, "none"),
        ("failed", "add", SOURCE, "event_msg", False, "none"),
        ("in_progress", "add", SOURCE, "event_msg", False, "none"),
        ("completed", "update", SOURCE, "event_msg", False, "none"),
        ("completed", "add", None, "event_msg", False, "none"),
        ("completed", "add", SOURCE, "response_item", False, "none"),
        ("completed", "add", SOURCE, "event_msg", True, "memory"),
        ("completed", "add", SOURCE, "event_msg", True, "durable"),
    ],
)
def test_native_add_confirms_red_time_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    kind: str,
    content: str | None,
    record_type: str,
    confirmed: bool,
    checkpoint: str,
) -> None:
    transcript = tmp_path / "codex.jsonl"
    records = [
        {
            "timestamp": "2026-10-14T21:00:01Z",
            "type": record_type,
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "FileChange",
                    "id": "native-add",
                    "status": status,
                    "changes": {str(tmp_path / TEST_PATH): {"type": kind, "content": content}},
                },
            },
        },
        {
            "timestamp": "2026-10-14T21:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "red-run",
                    "command": ["pytest", TEST_PATH, "-q"],
                    "cwd": str(tmp_path),
                    "status": "failed",
                    "exit_code": 1,
                    "stdout": RED,
                    "stderr": "",
                },
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    session = Session(
        id="author",
        external_id="synthetic-codex",
        machine_id="isolated",
        source="codex",
        project_id="test-project",
        title=None,
        status="active",
        transcript_path=str(transcript),
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=START,
        updated_at=START,
    )

    evidence, snapshot = _derive_transcript_path_evidence(
        session,
        str(transcript),
        START,
        ValidationDetectionConfig(),
        {TEST_PATH},
        str(tmp_path),
        frozenset({(str(tmp_path), TEST_PATH)}),
        [str(transcript)],
        resume=None,
    )

    if checkpoint != "none":
        assert snapshot is not None
        # Freeze the pre-FileChange fingerprint and state at EOF. The old parser
        # saw the RED but ignored the native add, so it retained no source edit.
        old_payload = json.dumps(
            {
                "derivation_version": 41,
                "session": session.id,
                "source": session.source,
                "window_start": START.isoformat(),
                "repo_path": str(tmp_path),
                "task_checkout_paths": [(str(tmp_path), TEST_PATH)],
                "task_edited_files": [TEST_PATH],
                "detection": ValidationDetectionConfig().model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        old_fingerprint = hashlib.sha256(old_payload.encode("utf-8")).hexdigest()
        resume = replace(snapshot, fingerprint=old_fingerprint, edits=())
        if checkpoint == "durable":
            monkeypatch.setattr(transcript_evidence_cache, "_cache_dir", lambda: tmp_path / "cache")
            key = f"{session.id}:{old_fingerprint}"
            store_durable_snapshot(key, resume)
            loaded = load_durable_snapshot(key)
            assert loaded == resume
            resume = loaded
        evidence, reparsed = _derive_transcript_path_evidence(
            session,
            str(transcript),
            START,
            ValidationDetectionConfig(),
            {TEST_PATH},
            str(tmp_path),
            frozenset({(str(tmp_path), TEST_PATH)}),
            [str(transcript)],
            resume=resume,
        )
        assert reparsed is not None
        assert reparsed.parsed_from_offset == 0

    run = evidence.validation_runs[0]
    test = AcceptanceTest(
        reference=f"{TEST_PATH}::test_feature",
        path=TEST_PATH,
        symbol="test_feature",
        body="def test_feature():\n    assert feature() == 2\n",
    )
    module = _original_test_module(test, evidence, run)
    assert (module is not None) is confirmed
    assert has_compacted_pytest_failure(run.output or "", test, module) is confirmed
    if confirmed:
        assert evidence.edits[0].source_after == SOURCE
        assert evidence.edits[0].source_confirmed_at == datetime(2026, 10, 14, 21, 0, 1, tzinfo=UTC)
        assert evidence.edits[0].source_confirmed_at is not None
        assert evidence.edits[0].source_confirmed_at < run.started_at
