from pathlib import Path

import pytest

from gobby.sessions.transcripts.base import TranscriptReadError, decode_transcript_record


def test_transcript_read_error_shared_by_digest_and_summary_readers() -> None:
    path = Path("transcript.jsonl")
    assert decode_transcript_record(
        b'{"valid":true}\n',
        path=path,
        byte_offset=41,
        line_number=3,
        is_final=True,
    ) == {"valid": True}

    corrupt_records = [
        (b'{"broken":}\n', False),
        (b'{"broken":}\n', True),
        (b'"scalar"\n', True),
        (b"[]\n", True),
        (b"\xff\n", False),
        (b"\xff\n", True),
        (b'"scalar"', True),
    ]
    for raw_record, is_final in corrupt_records:
        with pytest.raises(TranscriptReadError) as forward_error:
            decode_transcript_record(
                raw_record,
                path=path,
                byte_offset=41,
                line_number=3,
                is_final=is_final,
            )
        with pytest.raises(TranscriptReadError) as reverse_error:
            decode_transcript_record(
                raw_record,
                path=path,
                byte_offset=41,
                line_number=None,
                is_final=is_final,
            )

        assert (forward_error.value.path, forward_error.value.byte_offset) == (path, 41)
        assert forward_error.value.line_number == 3
        assert reverse_error.value.line_number is None

    assert (
        decode_transcript_record(
            b'{"unfinished":',
            path=path,
            byte_offset=41,
            line_number=3,
            is_final=True,
        )
        is None
    )
    assert (
        decode_transcript_record(
            b'{"split":"\xe2\x82',
            path=path,
            byte_offset=41,
            line_number=None,
            is_final=True,
        )
        is None
    )

    error = TranscriptReadError(path, byte_offset=41, line_number=3)
    assert "transcript.jsonl" in str(error)
    assert "byte 41" in str(error)
    assert "line 3" in str(error)
