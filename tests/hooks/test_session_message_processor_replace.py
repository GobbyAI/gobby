from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.hooks.event_handlers._session_start.transcripts import (
    replace_session_message_processor,
)

pytestmark = pytest.mark.unit


class _Processor:
    def __init__(self, name: str) -> None:
        self.name = name
        self.registered: list[tuple[str, str, str]] = []
        self.unregistered: list[str] = []

    def register_session(self, session_id: str, transcript_path: str, *, source: str) -> None:
        self.registered.append((session_id, transcript_path, source))

    def unregister_session(self, session_id: str) -> None:
        self.unregistered.append(session_id)


def test_replace_session_message_processor_unregisters_previous() -> None:
    previous = _Processor("old")
    replacement = _Processor("new")
    handler = SimpleNamespace(
        _session_message_processors={"sess": previous},
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
    )

    replace_session_message_processor(
        handler,
        "sess",
        replacement,
        "/tmp/transcript.jsonl",
        source="claude",
    )

    assert replacement.registered == [("sess", "/tmp/transcript.jsonl", "claude")]
    assert previous.unregistered == ["sess"]
    assert handler._session_message_processors["sess"] is replacement


def test_replace_session_message_processor_is_idempotent_for_same_processor() -> None:
    processor = _Processor("same")
    handler = SimpleNamespace(
        _session_message_processors={"sess": processor},
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
    )

    replace_session_message_processor(
        handler,
        "sess",
        processor,
        "/tmp/transcript.jsonl",
        source="claude",
    )

    assert processor.registered == [("sess", "/tmp/transcript.jsonl", "claude")]
    assert processor.unregistered == []
    assert handler._session_message_processors["sess"] is processor
