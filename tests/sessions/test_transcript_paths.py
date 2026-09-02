from pathlib import Path

from gobby.sessions.transcript_paths import find_supplemental_transcripts_on_disk


def test_claude_supplemental_transcripts_are_sorted_and_provider_scoped(tmp_path: Path) -> None:
    transcript = tmp_path / "session-id.jsonl"
    transcript.write_text("", encoding="utf-8")
    subagents = transcript.with_suffix("") / "subagents"
    subagents.mkdir(parents=True)
    later = subagents / "agent-z.jsonl"
    earlier = subagents / "agent-a.jsonl"
    metadata = subagents / "agent-a.meta.json"
    unrelated = subagents / "notes.jsonl"
    for path in (later, earlier, metadata, unrelated):
        path.write_text("{}\n", encoding="utf-8")

    assert find_supplemental_transcripts_on_disk("claude", str(transcript)) == [
        str(earlier),
        str(later),
    ]
    assert find_supplemental_transcripts_on_disk("codex", str(transcript)) == []
