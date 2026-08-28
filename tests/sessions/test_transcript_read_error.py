from pathlib import Path

import pytest

from gobby.sessions.summary_transcripts import _read_transcript
from gobby.sessions.transcripts.base import TranscriptReadError, decode_transcript_record


@pytest.mark.asyncio
async def test_archival_summary_reader_reports_corrupt_transcript_offsets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "transcript.jsonl"
    monkeypatch.setattr(
        "gobby.sessions.summary_transcripts.TRANSCRIPT_TAIL_RETRY_DELAY_SECONDS",
        0,
    )
    prefix = b'{"message":{"role":"user","content":"inspect"}}\n'
    path.write_bytes(prefix + b'{"broken":}\n')

    with pytest.raises(TranscriptReadError) as summary_error:
        await _read_transcript(path, "claude")

    assert summary_error.value.path == path
    assert summary_error.value.byte_offset == len(prefix)
    assert decode_transcript_record(
        b'{"valid":true}\n',
        path=path,
        byte_offset=41,
        line_number=3,
        is_final=True,
    ) == {"valid": True}
