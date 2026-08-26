from pathlib import Path

from gobby.sessions.transcripts.base import TranscriptReadError


def test_transcript_read_error_shared_by_digest_and_summary_readers() -> None:
    path = Path("transcript.jsonl")
    error = TranscriptReadError(path, byte_offset=41, line_number=3)
    reverse_error = TranscriptReadError(path, byte_offset=41)

    assert (error.path, error.byte_offset, error.line_number) == (path, 41, 3)
    assert "transcript.jsonl" in str(error)
    assert "byte 41" in str(error)
    assert "line 3" in str(error)
    assert reverse_error.line_number is None
