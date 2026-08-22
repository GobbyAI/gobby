"""Contract test for the AGY Gate 0 fixtures (plan agy-full-integration §1.1, task #19563).

Asserts the committed fixture set answers every probe record, covers both modes with
live camelCase payloads, carries a daemon-side receipt per mode and tool class, cites
a committed pane capture for every terminal-mode record, and is scrubbed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

AGY_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "provider_contracts" / "agy"
README = AGY_ROOT / "README.md"
RECORDS = [f"1.1.{n}" for n in range(1, 25)]
EVENTS = {"PreInvocation", "PreToolUse", "PostToolUse", "PostInvocation", "Stop"}
MODES = {"print", "interactive"}
TOOL_CLASSES = {"built-in", "shell", "mcp"}
VERDICT_RE = re.compile(r"\*\*(confirmed|re-confirmed|disproven|negative)[^*]*\*\*")
CAMEL_RE = re.compile(r"^[a-z]+(?:[A-Z][a-z0-9]*)*$")
ENVELOPE_RE = re.compile(r"^n-\d{13}-[0-9a-f-]{36}$")
SCRUB_CHECKS = {
    "absolute user path": re.compile(r"/Users/|/home/[a-z]"),
    "non-brain app-data path": re.compile(r"antigravity-cli/(?!brain/<CONVERSATION_ID>)"),
    "raw conversation id": re.compile(
        r"(?<![0-9a-f])(?<!n-\d{13}-)"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])"
    ),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}"),
}


def _jsonl(name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(
        (AGY_ROOT / name).read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        row = json.loads(line)
        assert isinstance(row, dict), f"{name}:{number} must be a JSON object"
        rows.append(row)
    return rows


def _outcome_table_rows() -> dict[str, tuple[str, str]]:
    """Map record id -> (outcome cell, summary cell) from the README outcome table."""
    text = README.read_text(encoding="utf-8")
    start = text.index("## Contract-outcome table")
    end = text.index("## Record evidence", start)
    rows: dict[str, tuple[str, str]] = {}
    for line in text[start:end].splitlines():
        match = re.match(r"\|\s*(1\.1\.\d+)\b[^|]*\|([^|]*)\|([^|]*)\|", line)
        if match:
            rows[match.group(1)] = (match.group(2).strip(), match.group(3).strip())
    return rows


def _fixture_files() -> list[Path]:
    return sorted(path for path in AGY_ROOT.rglob("*") if path.is_file())


def test_fixture_set_is_complete() -> None:
    expected = {
        "README.md",
        "hook-payloads.jsonl",
        "daemon-receipts.jsonl",
        "transcript-manifest.json",
        "stream-json-samples.jsonl",
        "command-captures.json",
    }
    present = {path.name for path in AGY_ROOT.iterdir() if path.is_file()}
    assert expected <= present, expected - present
    assert "agy_models_v1.0.10.txt" not in present
    assert "model-cache-summary.json" not in present
    assert (AGY_ROOT / "pane-captures").is_dir()


def test_outcome_table_covers_all_24_records_with_a_verdict() -> None:
    rows = _outcome_table_rows()
    assert sorted(rows, key=lambda r: int(r.rsplit(".", 1)[1])) == RECORDS
    for record, (outcome, summary) in rows.items():
        assert VERDICT_RE.search(outcome), f"{record} has no verdict: {outcome!r}"
        assert summary, f"{record} has no summary"


def test_record_evidence_sections_cover_all_24_records() -> None:
    text = README.read_text(encoding="utf-8")
    headings = set(re.findall(r"^### (1\.1\.\d+) — ", text, flags=re.MULTILINE))
    assert headings == set(RECORDS)
    assert "1.1.18" in text and "1.1.16" in text  # version history stated


@pytest.mark.parametrize("version_marker", ["1.1.18"])
def test_fixture_version_is_the_probed_floor(version_marker: str) -> None:
    for name in ("hook-payloads.jsonl", "daemon-receipts.jsonl", "stream-json-samples.jsonl"):
        for row in _jsonl(name):
            assert row.get("cli_version") == version_marker, (name, row.get("cli_version"))
    for name in ("transcript-manifest.json", "command-captures.json"):
        payload = json.loads((AGY_ROOT / name).read_text(encoding="utf-8"))
        assert payload["cli_version"] == version_marker, name


def test_hook_payloads_cover_five_events_in_both_modes_in_camel_case() -> None:
    rows = _jsonl("hook-payloads.jsonl")
    seen: dict[str, set[str]] = {mode: set() for mode in MODES}
    for row in rows:
        assert row["provider"] == "agy"
        assert row["mode"] in MODES, row["mode"]
        assert row["event"] in EVENTS, row["event"]
        assert row["capture_status"] == "live"
        payload = row["payload"]
        assert isinstance(payload, dict) and payload, row["event"]
        for key in payload:
            assert CAMEL_RE.match(key), f"non-camelCase payload key {key!r} in {row['event']}"
        assert payload["conversationId"] == "<CONVERSATION_ID>"
        assert row["env"]["ANTIGRAVITY_CONVERSATION_ID"] == "<CONVERSATION_ID>"
        seen[row["mode"]].add(row["event"])
    for mode in MODES:
        assert seen[mode] == EVENTS, (mode, EVENTS - seen[mode])
    assert not any("shape_only_not_live_proven" in json.dumps(row) for row in rows)


def test_hook_payloads_cover_each_tool_class_in_both_modes() -> None:
    tool_class = {"list_dir": "built-in", "run_command": "shell", "call_mcp_tool": "mcp"}
    seen: dict[str, set[str]] = {mode: set() for mode in MODES}
    for row in _jsonl("hook-payloads.jsonl"):
        if row["event"] != "PreToolUse":
            continue
        name = row["payload"]["toolCall"]["name"]
        if name in tool_class:
            seen[row["mode"]].add(tool_class[name])
    for mode in MODES:
        assert seen[mode] == TOOL_CLASSES, (mode, TOOL_CLASSES - seen[mode])


def test_daemon_receipts_exist_per_mode_and_tool_class() -> None:
    receipts = _jsonl("daemon-receipts.jsonl")
    assert receipts
    tool_seen: dict[str, set[str]] = {mode: set() for mode in MODES}
    event_seen: dict[str, set[str]] = {mode: set() for mode in MODES}
    envelope_ids: set[str] = set()
    for row in receipts:
        assert row["mode"] in MODES and row["event"] in EVENTS
        assert ENVELOPE_RE.match(row["envelope_id"]), row["envelope_id"]
        envelope_ids.add(row["envelope_id"])
        assert row["ghook_request"]["path"] == "/api/hooks/execute"
        assert row["payload"]["source"] == "agy"
        assert row["payload"]["hook_type"] == row["event"]
        assert "conversationId" in row["payload"]["input_data_keys"]
        assert row["daemon_http_status"] == 200, row
        assert isinstance(row["daemon_response"], dict)
        marker = row["daemon_processed_marker"]
        assert marker["envelope_id"] == row["envelope_id"]
        assert marker["status"] == "processed"
        assert marker["response"] == row["daemon_response"]
        assert row["daemon_hooks_log_line"], row["envelope_id"]
        assert row["ghook_exit"] == 0
        event_seen[row["mode"]].add(row["event"])
        if row["event"] in {"PreToolUse", "PostToolUse"}:
            assert row["tool_class"] in TOOL_CLASSES, row["tool_class"]
            tool_seen[row["mode"]].add(row["tool_class"])
    for mode in MODES:
        assert event_seen[mode] == EVENTS, (mode, EVENTS - event_seen[mode])
        assert tool_seen[mode] == TOOL_CLASSES, (mode, TOOL_CLASSES - tool_seen[mode])
    # every receipt joins to a committed hook payload line by envelope id
    payload_ids = {
        row["envelope_id"] for row in _jsonl("hook-payloads.jsonl") if "envelope_id" in row
    }
    assert envelope_ids <= payload_ids, envelope_ids - payload_ids


def test_terminal_mode_records_cite_committed_pane_captures() -> None:
    text = README.read_text(encoding="utf-8")
    cited = set(re.findall(r"pane-captures/(1\.1\.\d+-interactive[\w-]*)\.txt", text))
    # README also cites families like `1.1.14-interactive-*`; expand those
    families = {
        f"{record}-interactive"
        for record in re.findall(r"pane-captures/(1\.1\.\d+)-interactive-\*\.txt", text)
    }
    existing = {path.stem for path in (AGY_ROOT / "pane-captures").glob("*.txt")}
    brace_groups = re.findall(r"pane-captures/(1\.1\.\d+-interactive)-\{([\w,-]+)\}\.txt", text)
    for prefix, group in brace_groups:
        cited.update(f"{prefix}-{label}" for label in group.split(","))
    cited = {name for name in cited if "{" not in name}
    assert cited <= existing, cited - existing
    for record in {"1.1.3", "1.1.5", "1.1.7", "1.1.8", "1.1.14", "1.1.17", "1.1.23"}:
        assert any(name.startswith(f"{record}-interactive") for name in cited | families), record
        assert any(name.startswith(f"{record}-interactive") for name in existing), record
    for path in (AGY_ROOT / "pane-captures").glob("*.txt"):
        header = path.read_text(encoding="utf-8").splitlines()[:2]
        assert header[0].startswith("# AGY 1.1.18 interactive pane capture"), path.name
        assert "tmux capture-pane -p" in header[1], path.name


def test_isolated_home_models_probe_recorded() -> None:
    captures = json.loads((AGY_ROOT / "command-captures.json").read_text(encoding="utf-8"))
    unauth = [c for c in captures["commands"] if c["record"].startswith("1.1.20 unauthenticated")]
    assert unauth, "isolated-HOME models probe missing"
    for capture in unauth:
        assert "mktemp -d" in capture["command"]
        assert capture["agy_exit_codes"] and set(capture["agy_exit_codes"]) == {1}
        assert "Please sign in" in capture["stderr_tail"]
    rows = _outcome_table_rows()
    assert "isolated HOME" in rows["1.1.20"][1]


def test_gate0_capture_hook_removed_after_probe() -> None:
    captures = json.loads((AGY_ROOT / "command-captures.json").read_text(encoding="utf-8"))
    after = [c for c in captures["commands"] if "after gate0-capture removal" in c["record"]]
    assert after
    for capture in after:
        names = [h["name"] for h in capture["stdout"]["command"]["data"]["hooks"]]
        assert names == ["gobby"], names


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: str(p.relative_to(AGY_ROOT)))
def test_fixture_is_scrubbed(path: Path) -> None:
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for name, pattern in SCRUB_CHECKS.items():
            match = pattern.search(line)
            assert match is None, (
                f"{path.name}:{number}: {name}: {line[max(0, match.start() - 40) : match.end() + 20]!r}"
            )
