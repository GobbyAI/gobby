import json
from pathlib import Path
from typing import Any

import pytest

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


def _droid_epoch(path: Path, parent: object | None = None, *, id_field: str = "id") -> None:
    record: dict[str, object] = {"type": "session_start", id_field: path.stem}
    if parent is not None:
        record["parent"] = parent
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_droid_supplemental_transcripts_follow_only_parent_chain(tmp_path: Path) -> None:
    directory = tmp_path / ".factory" / "sessions" / "encoded-cwd"
    directory.mkdir(parents=True)
    oldest = directory / "oldest.jsonl"
    middle = directory / "middle.jsonl"
    current = directory / "current.jsonl"
    sibling = directory / "unrelated.jsonl"
    _droid_epoch(oldest)
    _droid_epoch(middle, "oldest")
    _droid_epoch(current, "middle")
    _droid_epoch(sibling)

    assert find_supplemental_transcripts_on_disk("droid", str(current)) == [
        str(oldest),
        str(middle),
    ]


def test_droid_supplemental_transcripts_accept_session_id_header(tmp_path: Path) -> None:
    parent = tmp_path / "parent.jsonl"
    current = tmp_path / "current.jsonl"
    _droid_epoch(parent, id_field="session_id")
    _droid_epoch(current, "parent", id_field="session_id")

    assert find_supplemental_transcripts_on_disk("droid", str(current)) == [str(parent)]


@pytest.mark.parametrize("parent", ["missing", "../outside", "/absolute", 123, ""])
def test_droid_supplemental_transcripts_stop_at_invalid_parent(
    tmp_path: Path, parent: object
) -> None:
    current = tmp_path / "current.jsonl"
    _droid_epoch(current, parent)

    assert find_supplemental_transcripts_on_disk("droid", str(current)) == []


def test_droid_supplemental_transcripts_stop_at_cycle_or_malformed_epoch(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current.jsonl"
    parent = tmp_path / "parent.jsonl"
    _droid_epoch(current, "parent")
    _droid_epoch(parent, "current")
    assert find_supplemental_transcripts_on_disk("droid", str(current)) == [str(parent)]

    parent.write_text("{invalid json}\n", encoding="utf-8")
    assert find_supplemental_transcripts_on_disk("droid", str(current)) == []


def test_droid_supplemental_transcripts_reject_mismatched_parent_header(tmp_path: Path) -> None:
    current = tmp_path / "current.jsonl"
    parent = tmp_path / "parent.jsonl"
    _droid_epoch(current, "parent")
    parent.write_text(
        json.dumps({"type": "session_start", "id": "unrelated"}) + "\n",
        encoding="utf-8",
    )

    assert find_supplemental_transcripts_on_disk("droid", str(current)) == []


def test_droid_supplemental_transcripts_reject_alias_of_current_epoch(tmp_path: Path) -> None:
    current = tmp_path / "current.jsonl"
    _droid_epoch(current, "alias")
    (tmp_path / "alias.jsonl").symlink_to(current)

    assert find_supplemental_transcripts_on_disk("droid", str(current)) == []


def test_droid_supplemental_transcripts_reject_parent_symlink_outside_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "encoded-cwd"
    directory.mkdir()
    current = directory / "current.jsonl"
    outside = tmp_path / "outside.jsonl"
    _droid_epoch(current, "parent")
    _droid_epoch(outside)
    (directory / "parent.jsonl").symlink_to(outside)

    assert find_supplemental_transcripts_on_disk("droid", str(current)) == []


def test_droid_supplemental_transcripts_stop_at_unreadable_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = tmp_path / "current.jsonl"
    parent = tmp_path / "parent.jsonl"
    _droid_epoch(current, "parent")
    _droid_epoch(parent)
    original_open = Path.open

    def deny_parent_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == parent:
            raise PermissionError("parent transcript is unreadable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_parent_open)
    assert find_supplemental_transcripts_on_disk("droid", str(current)) == []
