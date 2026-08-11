import json
import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.sessions.token_usage import typed_json_token_usage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.config_runtime_helpers import static_session_capture

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-4000-8000-000000000101"

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-00000000001a"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def test_typed_json_token_usage_warns_when_cache_read_exceeds_prompt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="gobby.sessions.token_usage"):
        usage = typed_json_token_usage(
            {
                "promptTokenCount": 10,
                "cachedContentTokenCount": 25,
                "candidatesTokenCount": 3,
            }
        )

    assert usage.input_tokens == 0
    assert usage.cache_read_tokens == 25
    assert any("exceeds promptTokenCount" in record.message for record in caplog.records)


@pytest.fixture
def db(tmp_path: Path, hub_db: HubDatabase) -> HubDatabase:
    """Initialize database with migrations."""
    database = hub_db
    # Create dummy project required for sessions
    database.execute(
        "INSERT INTO projects (id, name, repo_path) VALUES (%s, %s, %s)",
        (PROJECT_ID, "Test Project", str(tmp_path)),
    )
    return database


@pytest.fixture
def session_manager(db: HubDatabase) -> SessionManager:
    return SessionManager(db)


@pytest.fixture
def lifecycle_manager(db: HubDatabase) -> SessionLifecycleManager:
    config = SessionLifecycleConfig()
    return SessionLifecycleManager(db, static_session_capture(config))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_token_usage_aggregation(
    db: HubDatabase,
    session_manager: SessionManager,
    lifecycle_manager: SessionLifecycleManager,
    tmp_path: Path,
) -> None:
    """Test that token usage is correctly aggregated from transcript files."""

    # 1. Create a dummy transcript with usage data
    # Format matches what ClaudeTranscriptParser expects
    transcript_path = tmp_path / "transcript.jsonl"

    transcript_data = [
        # Msg 1: Assistant msg with top-level usage
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
            "usage": {"input_tokens": 10, "output_tokens": 20, "cost": 0.001},
        },
        # Msg 2: User msg with top-level usage
        {
            "type": "user",
            "message": {"role": "user", "content": "Hi"},
            "usage": {"input_tokens": 5, "output_tokens": 0},
        },
        # Msg 3: Assistant msg with nested usage (Claude API style)
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Bye"}],
                "usage": {"input_tokens": 15, "output_tokens": 25, "cost": 0.002},
            },
        },
    ]

    with open(transcript_path, "w") as f:
        for entry in transcript_data:
            f.write(json.dumps(entry) + "\n")

    # 2. Register a session
    session = session_manager.register(
        external_id="ext-123",
        machine_id="21000000-0000-4000-8000-00000000001a",
        source="claude_code",
        project_id=PROJECT_ID,
        title="Test Session",
        transcript_path=str(transcript_path),
    )

    # 3. Process the transcript
    # We call the internal method directly to bypass status checks for testing
    await lifecycle_manager._process_session_transcript(session.id, str(transcript_path))

    # 4. Verify results
    updated_session = session_manager.get(session.id)

    assert updated_session is not None

    # Expected:
    # 1: 10 in, 20 out
    # 2: 5 in, 0 out
    # 3: 15 in, 25 out
    # Total: 30 in, 45 out

    assert updated_session.usage_input_tokens == 30
    assert updated_session.usage_output_tokens == 45
