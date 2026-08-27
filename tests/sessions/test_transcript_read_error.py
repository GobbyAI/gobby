from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.digest import _read_undigested_turns, build_turn_and_digest
from gobby.sessions.summary_transcripts import _read_transcript
from gobby.sessions.transcripts.base import TranscriptReadError, decode_transcript_record


@pytest.mark.asyncio
async def test_transcript_read_error_shared_by_digest_and_summary_readers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "transcript.jsonl"
    monkeypatch.setattr("gobby.memory.digest.TRANSCRIPT_TAIL_RETRY_DELAY_SECONDS", 0, raising=False)
    monkeypatch.setattr(
        "gobby.sessions.summary_transcripts.TRANSCRIPT_TAIL_RETRY_DELAY_SECONDS",
        0,
        raising=False,
    )
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

    prefix = b'{"message":{"role":"user","content":"inspect"}}\n'
    corruption_cases = [
        b'{"broken":}\n',
        b'"scalar"\n',
        b"[]\n",
        b"\xff\n",
        b'{"broken":}\n{"message":{"role":"assistant","content":"after"}}\n',
    ]
    for corrupt in corruption_cases:
        path.write_bytes(prefix + corrupt)
        summary_limit = None if b'"after"' in corrupt else 1
        with pytest.raises(TranscriptReadError) as digest_error:
            await _read_undigested_turns(str(path), "claude", 0)
        with pytest.raises(TranscriptReadError) as summary_error:
            await _read_transcript(path, "claude", max_turns=summary_limit)

        assert digest_error.value.path == summary_error.value.path == path
        assert digest_error.value.byte_offset == summary_error.value.byte_offset == len(prefix)
        assert digest_error.value.line_number == 2
        assert summary_error.value.line_number is None

    stable_prefix = (
        b'{"message":{"role":"user","content":"previous"}}\n'
        b'{"message":{"role":"assistant","content":"done"}}\n'
        b'{"message":{"role":"user","content":"current"}}\n'
    )
    for partial in (b'{"unfinished":', b'{"split":"\xe2\x82'):
        path.write_bytes(stable_prefix + partial)

        pairs, next_index = await _read_undigested_turns(str(path), "claude", 0)
        summary_turns = await _read_transcript(path, "claude")

        assert pairs == [("previous", "done")]
        assert next_index == 1
        assert [turn["message"]["content"] for turn in summary_turns] == [
            "previous",
            "done",
            "current",
        ]

    path.write_bytes(b'{"broken":}\n')
    memory_manager = MagicMock(config=SimpleNamespace(enabled=True), db=MagicMock())
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(
        transcript_path=str(path),
        source="claude",
        last_digested_pair_index=0,
        last_digest_input_hash=None,
    )
    result = await build_turn_and_digest(
        memory_manager=memory_manager,
        session_manager=session_manager,
        session_id="session-1",
        llm_service=AsyncMock(),
        config=SimpleNamespace(digest=SimpleNamespace(enabled=True, num_pairs=50)),
    )

    assert result is not None
    assert result["error_kind"] == "transcript_read"
