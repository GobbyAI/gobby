"""Archive-backed scheduled retries, fairness, quarantine, and source recovery."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.sessions.summarize import generate_session_summaries
from gobby.sessions.summary_transcripts import _read_transcript_window
from gobby.sessions.transcripts.base import TranscriptReadError
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from tests.config_runtime_helpers import static_session_capture
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit
MACHINE_ID = "20000000-0000-4000-8000-000000000002"
GOLDEN = Path(__file__).parent / "transcripts/fixtures/golden_path/claude.jsonl"


@pytest.fixture
def lifecycle(temp_db: HubDatabase, tmp_path: Path) -> Iterator[SessionLifecycleManager]:
    manager = SessionLifecycleManager(
        temp_db,
        static_session_capture(
            SessionLifecycleConfig(
                transcript_processing_batch_size=2,
                transcript_archive_dir=str(tmp_path / "archives"),
            )
        ),
    )
    with (
        patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID),
        patch("gobby.sessions.transcript_reader.find_transcript_on_disk", return_value=None),
        patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
    ):
        yield manager


def register(
    lifecycle: SessionLifecycleManager,
    project_id: str,
    name: str,
    path: str | None = None,
    source: str = "claude",
) -> Session:
    session = lifecycle.session_manager.register(
        external_id=name,
        machine_id=MACHINE_ID,
        source=source,
        project_id=project_id,
        transcript_path=path,
    )
    lifecycle.session_manager.update_status(session.id, "expired")
    result = lifecycle.session_manager.get(session.id)
    assert result is not None
    return result


def processed(lifecycle: SessionLifecycleManager, session_id: str) -> bool:
    row = lifecycle.db.fetchone(
        "SELECT transcript_processed FROM sessions WHERE id = %s", (session_id,)
    )
    assert row is not None
    return bool(row["transcript_processed"])


async def test_null_path_archive_reaches_canonical_summary(
    lifecycle: SessionLifecycleManager,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project = isolated_checkout_factory(temp_db, "archive-summary").project
    session = register(lifecycle, project.id, "archive-only")
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    (archive_dir / "archive-only.jsonl.gz").write_bytes(gzip.compress(GOLDEN.read_bytes()))
    assert await lifecycle._process_pending_transcripts(lifecycle._capture_active()) == 1
    saved = lifecycle.session_manager.get(session.id)
    assert saved is not None
    assert saved.summary_markdown and saved.summary_source_context_hash
    assert saved.transcript_path is None
    assert processed(lifecycle, session.id)


async def test_poison_fairness_equal_timestamps_wrap_and_new_arrivals(
    lifecycle: SessionLifecycleManager,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
) -> None:
    project = isolated_checkout_factory(temp_db, "fair-retries").project
    sessions = [register(lifecycle, project.id, f"missing-{i}") for i in range(5)]
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    with temp_db.transaction() as conn:
        for session in sessions:
            conn.execute("UPDATE sessions SET created_at = %s WHERE id = %s", (stamp, session.id))
    ordered = sorted(sessions, key=lambda session: session.id)
    attempted: list[str] = []
    with patch.object(lifecycle, "_process_session_transcript", new_callable=AsyncMock) as parse:
        parse.side_effect = lambda session_id, path: attempted.append(session_id)
        for _ in range(3):
            before = len(attempted)
            assert await lifecycle._process_pending_transcripts(lifecycle._capture_active()) == 0
            assert 0 < len(attempted) - before <= 2
        assert attempted == [s.id for s in ordered]
        await lifecycle._process_pending_transcripts(lifecycle._capture_active())
        assert attempted[5:] == [s.id for s in ordered[:2]]
        newcomer = register(lifecycle, project.id, "new-arrival")
        with temp_db.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET created_at = %s WHERE id = %s",
                (datetime(2025, 1, 1, tzinfo=UTC), newcomer.id),
            )
        for _ in range(3):
            await lifecycle._process_pending_transcripts(lifecycle._capture_active())
        assert newcomer.id in attempted


async def test_quarantine_keeps_missing_sessions_unprocessed(
    lifecycle: SessionLifecycleManager,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
) -> None:
    project = isolated_checkout_factory(temp_db, "quarantine").project
    session = register(lifecycle, project.id, "missing")
    for _ in range(4):
        assert await lifecycle._process_pending_transcripts(lifecycle._capture_active()) == 0
    saved = lifecycle.session_manager.get(session.id)
    assert saved is not None
    assert saved.transcript_processing_failure_count == 3
    assert saved.transcript_processing_last_error_code == "missing_source"
    assert saved.transcript_processing_last_error == "Transcript file not found"
    assert saved.transcript_processing_last_failed_at is not None
    assert not processed(lifecycle, session.id)
    assert lifecycle.session_manager.get_pending_transcript_sessions() == []


@pytest.mark.parametrize(
    "error", [TimeoutError("provider timeout"), PermissionError("busy source")]
)
async def test_transient_failures_do_not_consume_quarantine_budget(
    lifecycle: SessionLifecycleManager,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    error: Exception,
) -> None:
    project = isolated_checkout_factory(temp_db, "transient").project
    session = register(lifecycle, project.id, "transient")
    with patch.object(
        lifecycle, "_generate_artifacts_if_needed", new_callable=AsyncMock, side_effect=error
    ):
        for _ in range(4):
            assert await lifecycle._process_pending_transcripts(lifecycle._capture_active()) == 0
    saved = lifecycle.session_manager.get(session.id)
    assert saved is not None and saved.transcript_processing_failure_count == 0
    assert not processed(lifecycle, session.id)


@pytest.mark.parametrize(
    "recovery", ["path", "source", "external_id", "revive", "reactivate", "restore"]
)
def test_recovery_resets_failure_metadata(
    lifecycle: SessionLifecycleManager,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    recovery: str,
) -> None:
    project = isolated_checkout_factory(temp_db, "recovery").project
    session = register(lifecycle, project.id, "recover")
    manager = lifecycle.session_manager
    for _ in range(3):
        manager.record_transcript_processing_failure(
            session.id, error_code="missing_source", error="missing"
        )
    if recovery == "path":
        manager.update(session.id, transcript_path=str(GOLDEN))
    elif recovery == "source":
        manager.update(session.id, source="codex")
    elif recovery == "external_id":
        manager.update(session.id, external_id="replacement")
    elif recovery == "revive":
        manager.revive_expired_terminal_session(session.id)
    elif recovery == "reactivate":
        # Native writers also reactivate rows; exercise the common DB contract.
        with temp_db.transaction() as conn:
            conn.execute("UPDATE sessions SET status = 'active' WHERE id = %s", (session.id,))
    else:
        manager.reset_transcript_processed(session.id)
    saved = manager.get(session.id)
    assert saved is not None
    assert saved.transcript_processing_failure_count == 0
    assert saved.transcript_processing_last_error_code is None
    assert saved.transcript_processing_last_error is None
    assert saved.transcript_processing_last_failed_at is None
    assert not processed(lifecycle, session.id)


async def test_failed_refresh_preserves_valid_summary_and_success_resets_failures(
    lifecycle: SessionLifecycleManager,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project = isolated_checkout_factory(temp_db, "freshness").project
    transcript = tmp_path / "live.jsonl"
    transcript.write_bytes(GOLDEN.read_bytes())
    session = register(lifecycle, project.id, "refresh", str(transcript))
    manager = lifecycle.session_manager
    first = await generate_session_summaries(
        session.id,
        manager,
        session_summary_config=lifecycle._capture_active().session_summary,
        db=temp_db,
    )
    assert first["success"]
    saved = manager.get(session.id)
    assert saved is not None
    original = (
        saved.summary_markdown,
        saved.summary_source_context_hash,
        saved.summary_generated_at,
    )
    transcript.write_bytes(b"{broken}\n")
    assert await lifecycle._process_pending_transcripts(lifecycle._capture_active()) == 0
    failed = manager.get(session.id)
    assert failed is not None
    assert failed.transcript_processing_failure_count == 1
    assert failed.transcript_processing_last_error_code == "corrupt_source"
    assert (
        failed.summary_markdown,
        failed.summary_source_context_hash,
        failed.summary_generated_at,
    ) == original
    assert not processed(lifecycle, session.id)
    transcript.write_bytes(GOLDEN.read_bytes())
    assert await lifecycle._process_pending_transcripts(lifecycle._capture_active()) == 1
    recovered = manager.get(session.id)
    assert recovered is not None and recovered.transcript_processing_failure_count == 0
    assert recovered.summary_generated_at == original[2]


async def test_bounded_archive_window_and_corruption(tmp_path: Path) -> None:
    archive = tmp_path / "records.jsonl.gz"
    archive.write_bytes(gzip.compress(b"".join(f'{{"number":{i}}}\n'.encode() for i in range(100))))
    window = await _read_transcript_window(archive, source="claude", max_records=3)
    assert window.truncated
    assert [turn["number"] for turn in window.turns] == [97, 98, 99]
    archive.write_bytes(gzip.compress(b'{"number":1}\ninvalid\n'))
    with pytest.raises(TranscriptReadError):
        await _read_transcript_window(archive, source="claude", max_records=3)
    archive.write_bytes(b"bad gzip")
    with pytest.raises(TranscriptReadError):
        await _read_transcript_window(archive, source="claude", max_records=3)


def test_stale_attempt_cannot_complete_or_quarantine_replaced_source(
    lifecycle: SessionLifecycleManager,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
) -> None:
    project = isolated_checkout_factory(temp_db, "stale-attempt").project
    session = register(lifecycle, project.id, "stale")
    manager = lifecycle.session_manager
    manager.update(session.id, transcript_path="/new/path")
    manager.record_transcript_processing_failure(
        session.id, error_code="missing_source", error="old source", expected_session=session
    )
    assert manager.mark_transcript_processed(session.id, expected_session=session) is None
    saved = manager.get(session.id)
    assert saved is not None and saved.transcript_processing_failure_count == 0
    assert not processed(lifecycle, session.id)


@pytest.mark.parametrize("failure", ["unsupported", "empty", "invalid_output", "provider"])
async def test_canonical_failures_preserve_summaries_and_classify_retry_budget(
    lifecycle: SessionLifecycleManager,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    tmp_path: Path,
    failure: str,
) -> None:
    project = isolated_checkout_factory(temp_db, "canonical-failures").project
    transcript = tmp_path / "source.jsonl"
    transcript.write_bytes(GOLDEN.read_bytes())
    session = register(lifecycle, project.id, "canonical-failure", str(transcript))
    manager = lifecycle.session_manager
    config = lifecycle._capture_active()
    result = await generate_session_summaries(
        session.id,
        manager,
        session_summary_config=config.session_summary,
        db=temp_db,
    )
    assert result["success"]
    original = manager.get(session.id)
    assert original is not None
    if failure == "unsupported":
        manager.update(session.id, source="unsupported-provider")
    elif failure == "empty":
        transcript.write_text("")
    else:
        with transcript.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": "Implement a completely different new requirement.",
                        },
                    }
                )
                + "\n"
            )
        manager.update_stats(session.id, message_count=8, turn_count=4, tool_call_count=0)
        llm = SimpleNamespace(call_feature=AsyncMock())
        if failure == "provider":
            llm.call_feature.side_effect = TimeoutError("provider temporarily unavailable")
        else:
            llm.call_feature.return_value = "invalid response"
        lifecycle._capture_bundle = static_session_capture(
            config.session_lifecycle,
            session_summary=config.session_summary,
            services={"ai_services": SimpleNamespace(llm_service=llm)},
        )
    for _ in range(3):
        assert await lifecycle._process_pending_transcripts(config) == 0
    current = manager.get(session.id)
    assert current is not None
    assert current.summary_markdown == original.summary_markdown
    assert current.summary_source_context_hash == original.summary_source_context_hash
    assert current.summary_generated_at == original.summary_generated_at
    assert not processed(lifecycle, session.id)
    expected = {
        "unsupported": "unsupported_source",
        "empty": "invalid_summary",
        "invalid_output": "invalid_summary",
        "provider": None,
    }[failure]
    assert current.transcript_processing_last_error_code == expected
    assert current.transcript_processing_failure_count == (0 if expected is None else 3)


async def test_archive_copy_does_not_reset_quarantine_but_explicit_success_does(
    lifecycle: SessionLifecycleManager,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project = isolated_checkout_factory(temp_db, "explicit-recovery").project
    session = register(lifecycle, project.id, "copied")
    manager = lifecycle.session_manager
    for _ in range(3):
        await lifecycle._process_pending_transcripts(lifecycle._capture_active())
    archive = tmp_path / "archives/copied.jsonl.gz"
    archive.write_bytes(gzip.compress(GOLDEN.read_bytes()))
    assert await lifecycle._process_pending_transcripts(lifecycle._capture_active()) == 0
    assert manager.get_pending_transcript_sessions() == []
    result = await lifecycle._generate_artifacts_if_needed(
        session.id,
        lifecycle._capture_active().session_summary,
        allow_llm=False,
        archive_dir=str(archive.parent),
    )
    assert result["success"]
    assert [s.id for s in manager.get_pending_transcript_sessions()] == [session.id]
    # Explicit hash-matched reuse also clears quarantine without rewriting the summary.
    manager.record_transcript_processing_failure(
        session.id, error_code="missing_source", error="old failure"
    )
    original = manager.get(session.id)
    assert original is not None
    result = await lifecycle._generate_artifacts_if_needed(
        session.id,
        lifecycle._capture_active().session_summary,
        allow_llm=False,
        archive_dir=str(archive.parent),
    )
    assert result["generation_mode"] == "noop"
    current = manager.get(session.id)
    assert current is not None
    assert current.summary_generated_at == original.summary_generated_at
    assert current.transcript_processing_failure_count == 0


@pytest.mark.parametrize("change", ["path", "source", "external_id", "active", "paused", "summary"])
@pytest.mark.parametrize("scheduled", [False, True])
async def test_stale_generation_preserves_replacement_summary(
    lifecycle: SessionLifecycleManager,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    tmp_path: Path,
    change: str,
    scheduled: bool,
) -> None:
    project = isolated_checkout_factory(temp_db, "summary-race").project
    transcript = tmp_path / "live.jsonl"
    transcript.write_bytes(GOLDEN.read_bytes())
    session = register(lifecycle, project.id, "summary-race", str(transcript))
    manager = lifecycle.session_manager
    replacement = (
        "## Current State\n\nThe replacement session has completed its implementation and "
        "preserved its new source details for review.\n\n## Next Steps\n\n"
        "Review the replacement source and validate its current implementation."
    )
    stale = replacement.replace("replacement", "outdated")

    def replace_source(**kwargs: object) -> tuple[str, None]:
        if change == "path":
            manager.update(session.id, transcript_path=str(tmp_path / "replacement.jsonl"))
        elif change == "source":
            manager.update(session.id, source="codex")
        elif change == "external_id":
            manager.update(session.id, external_id="replacement-id")
        elif change in {"active", "paused"}:
            with temp_db.transaction() as conn:
                conn.execute("UPDATE sessions SET status = %s WHERE id = %s", (change, session.id))
        manager.persist_summary_state(
            session.id,
            summary_markdown=replacement,
            generation_mode="full",
            source_context_hash="replacement-hash",
        )
        manager.record_transcript_processing_failure(
            session.id, error_code="missing_source", error="replacement unavailable"
        )
        return stale, None

    with patch(
        "gobby.sessions.summarize._generate_full_summary",
        new=AsyncMock(side_effect=replace_source),
    ):
        if scheduled:
            assert await lifecycle._process_pending_transcripts(lifecycle._capture_active()) == 0
        else:
            result = await generate_session_summaries(
                session.id,
                manager,
                session_summary_config=lifecycle._capture_active().session_summary,
                db=temp_db,
            )
            assert not result["success"]
    saved = manager.get(session.id)
    assert saved is not None
    assert saved.summary_markdown == replacement
    assert saved.summary_source_context_hash == "replacement-hash"
    assert saved.transcript_processing_failure_count == (0 if change in {"active", "paused"} else 1)
    assert not processed(lifecycle, session.id)


async def test_archive_access_failure_is_transient_at_reader_boundary(
    lifecycle: SessionLifecycleManager,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
) -> None:
    project = isolated_checkout_factory(temp_db, "archive-access").project
    session = register(lifecycle, project.id, "unreadable")
    with patch(
        "gobby.sessions.transcript_reader._summary_source_exists",
        side_effect=PermissionError("denied"),
    ):
        assert await lifecycle._process_pending_transcripts(lifecycle._capture_active()) == 0
    saved = lifecycle.session_manager.get(session.id)
    assert saved is not None
    assert saved.transcript_processing_failure_count == 0
    assert not processed(lifecycle, session.id)
