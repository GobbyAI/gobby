"""Contract test for the AGY Gate 0 fixtures (plan agy-full-integration §1.1, task #19563).

Asserts the committed fixture set answers every probe record with a literal command and
observed output, covers both modes with live camelCase payloads, carries a daemon-side
receipt per mode and tool class, cites a print artifact and a committed pane capture
for every live-turn record (the live-record set is derived from the README outcome
table's ``Modes`` column), keeps every pane capture's body real, correlates every
evidence file's command with its README record and every cited helper with a committed,
parseable ``probe/`` script, keeps every free-text prompt inside the README's enumerated
probe-prompt list, and is scrubbed.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

AGY_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "provider_contracts" / "agy"
README = AGY_ROOT / "README.md"
PANES = AGY_ROOT / "pane-captures"
EVIDENCE = AGY_ROOT / "evidence"
RECORDS = [f"1.1.{n}" for n in range(1, 25)]
EVENTS = {"PreInvocation", "PreToolUse", "PostToolUse", "PostInvocation", "Stop"}
MODES = {"print", "interactive"}
MODE_CELLS = {"both", "print", "command"}
TOOL_CLASSES = {"built-in", "shell", "mcp"}
PRINT_ARTIFACTS = (
    "evidence/",
    "hook-payloads.jsonl",
    "daemon-receipts.jsonl",
    "stream-json-samples.jsonl",
    "command-captures.json",
    "transcript-manifest.json",
)
VERDICT_RE = re.compile(r"\*\*(confirmed|re-confirmed|disproven|negative)[^*]*\*\*")
CAMEL_RE = re.compile(r"^[a-z]+(?:[A-Z][a-z0-9]*)*$")
ENVELOPE_RE = re.compile(r"^n-\d{13}-[0-9a-f-]{36}$")
TABLE_ROW_RE = re.compile(r"\|\s*(1\.1\.\d+)\b[^|]*\|([^|]*)\|([^|]*)\|([^|]*)\|")
PANE_CITE_RE = re.compile(r"(?:pane-captures/|`|\(|\s)(1\.1\.\d+-interactive[\w-]*)\.txt")
PANE_FAMILY_RE = re.compile(r"pane-captures/(1\.1\.\d+)-interactive-\*\.txt")
PANE_BRACE_RE = re.compile(r"pane-captures/(1\.1\.\d+-interactive)-\{([\w,-]+)\}\.txt")
EVIDENCE_CITE_RE = re.compile(r"evidence/(1\.1\.\d+-[\w.-]+)\.txt")
HELPER_CITE_RE = re.compile(r"(?:^|\s)(?:bash|python3)\s+(\S+\.(?:sh|py))\b")
ANNOTATED_COMMAND_RE = re.compile(r"\s{2,}\(.*\)\s*$")
COMMENT_RE = re.compile(r"\s{2,}#.*$")
# README shorthands for the common print-mode flag sets (defined in "Record evidence"
# and in record 1.1.24); evidence files spell them out.
FLAG_SHORTHANDS = (
    (
        "$F'",
        "--output-format stream-json --sandbox=false --add-dir <WORKSPACE> --print-timeout 3m"
        " --dangerously-skip-permissions",
    ),
    (
        "$F",
        "--output-format stream-json --sandbox=false --dangerously-skip-permissions"
        " --print-timeout 4m --add-dir <WORKSPACE>",
    ),
)
GROUNDING_RE = re.compile(
    r"(^|[\s;|(/])(agy|antigravity|tmux)\b|<WORKSPACE>|<PROBE_SCRATCH>|~/\.gemini/"
)
# Prompt positions: `-p` arguments, `send-keys -l` arguments, stream-json stdin lines
# logged by probe/inputfmt.py, and the interactive pane's `> ` echo of the user line.
SQ, DQ = r"'([^']*)'", r'"((?:[^"\\]|\\.)*)"'
PROMPT_ARG_RE = re.compile(rf"(?:^|\s)-p\s+(?:{SQ}|{DQ})")
SENDKEYS_ARG_RE = re.compile(rf"send-keys\s+-t\s+\S+\s+-l\s+(?:{SQ}|{DQ})")
STDIN_LINE_RE = re.compile(r"^\[\s*[\d.]+\]\s>> (.*)$", re.MULTILINE)
PANE_ECHO_RE = re.compile(r"^> (.+)$", re.MULTILINE)
BARE_SLASH_COMMAND_RE = re.compile(r"^/[a-z-]+$")
# Pane lines rendered after the `> ` glyph that are UI, not user prompts: the slash-command
# menu, a numbered permission choice, the plan-mode status line, an approved-plan marker.
PANE_UI_RE = re.compile(r"^(/\S*(\s{2,}.*)?|\d+\. \S.*|Plan mode: .*|\[Approved\] .*)$")
SCRUB_CHECKS = {
    "absolute user path": re.compile(r"/Users/|/home/[a-z]"),
    "non-brain app-data path": re.compile(r"antigravity-cli/(?!brain/<CONVERSATION_ID>)"),
    "raw conversation id": re.compile(
        r"(?<![0-9a-f])(?<!n-\d{13}-)"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])"
    ),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}"),
    "oauth client/challenge/state": re.compile(
        r"(client_id|code_challenge|state)=(?!<REDACTED>)[^&\s]"
    ),
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


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _outcome_table_rows() -> dict[str, tuple[str, str, str]]:
    """Map record id -> (modes cell, outcome cell, summary cell) from the README table."""
    text = _readme()
    start = text.index("## Contract-outcome table")
    end = text.index("## Record evidence", start)
    rows: dict[str, tuple[str, str, str]] = {}
    for line in text[start:end].splitlines():
        match = TABLE_ROW_RE.match(line)
        if match:
            record, modes, outcome, summary = match.groups()
            rows[record] = (modes.strip(), outcome.strip(), summary.strip())
    return rows


def _live_records() -> set[str]:
    """Records the README table marks as answered in both modes (live agent turns)."""
    return {record for record, (modes, _, _) in _outcome_table_rows().items() if modes == "both"}


def _record_sections() -> dict[str, str]:
    """Map record id -> its '### 1.1.N — …' evidence section text."""
    text = _readme()
    start = text.index("## Record evidence")
    end = text.index("## Negative contracts", start)
    body = text[start:end]
    parts = re.split(r"^### (1\.1\.\d+) — [^\n]*\n", body, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def _fenced_lines(section: str) -> list[str]:
    lines: list[str] = []
    in_fence = False
    for line in section.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            lines.append(line)
    return lines


def _pane_cites(section: str) -> set[str]:
    cited = set(PANE_CITE_RE.findall(section))
    for prefix, group in PANE_BRACE_RE.findall(section):
        cited.update(f"{prefix}-{label}" for label in group.split(","))
    cited = {name for name in cited if "{" not in name}
    for record in PANE_FAMILY_RE.findall(section):
        cited.update(p.stem for p in PANES.glob(f"{record}-interactive-*.txt"))
    return cited


def _fixture_files() -> list[Path]:
    return sorted(path for path in AGY_ROOT.rglob("*") if path.is_file())


def _pane_body(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 3 and all(line.startswith("# ") for line in lines[:3]), path.name
    assert lines[0].startswith("# AGY 1.1.18 interactive pane capture"), path.name
    assert "tmux capture-pane -p" in lines[1], path.name
    return [line for line in lines[3:] if line.strip() and not line.startswith("# ")]


def _command(line: str) -> str:
    """A `$ ` line with its trailing `  # comment` dropped and README shorthands expanded."""
    text = COMMENT_RE.sub("", line).rstrip()
    for short, full in FLAG_SHORTHANDS:
        text = text.replace(short, full)
    return text


def _quoted(match: re.Match[str]) -> str:
    single, double = match.group(1), match.group(2)
    return single if single is not None else double.replace('\\"', '"')


def _stdin_prompt(raw: str) -> str:
    """The prompt text inside a stream-json stdin line (raw text when it is not JSON)."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(obj, dict):
        return raw
    if "prompt" in obj:
        return str(obj["prompt"])
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(str(t) for t in texts)
    return ""


def _probe_prompts() -> list[str]:
    text = _readme()
    start = text.index("## Probe prompts")
    end = text.index("## ", start + 1)
    return _fenced_lines(text[start:end])


def _prompt_positions(path: Path) -> list[tuple[int, str]]:
    """Every (line number, prompt text) position in a fixture file."""
    text = path.read_text(encoding="utf-8")
    found: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        found.extend((number, _quoted(m)) for m in PROMPT_ARG_RE.finditer(line))
        found.extend((number, _quoted(m)) for m in SENDKEYS_ARG_RE.finditer(line))
        stdin = STDIN_LINE_RE.match(line)
        if stdin:
            found.append((number, _stdin_prompt(stdin.group(1))))
        echo = PANE_ECHO_RE.match(line)
        if echo and path.parent == PANES and not PANE_UI_RE.match(echo.group(1).rstrip()):
            found.append((number, echo.group(1).rstrip()))
    return [(number, prompt) for number, prompt in found if prompt]


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
    assert PANES.is_dir() and EVIDENCE.is_dir()


def test_outcome_table_covers_all_24_records_with_a_verdict_and_a_mode() -> None:
    rows = _outcome_table_rows()
    assert sorted(rows, key=_record_order) == RECORDS
    for record, (modes, outcome, summary) in rows.items():
        assert modes in MODE_CELLS, f"{record} has an unknown Modes cell: {modes!r}"
        assert VERDICT_RE.search(outcome), f"{record} has no verdict: {outcome!r}"
        assert summary, f"{record} has no summary"
    live = _live_records()
    assert len(live) >= 15, sorted(live)
    assert {"1.1.5", "1.1.8", "1.1.17", "1.1.24"} <= live


def test_record_evidence_sections_cover_all_24_records() -> None:
    sections = _record_sections()
    assert set(sections) == set(RECORDS)
    text = _readme()
    assert "1.1.18" in text and "1.1.16" in text  # version history stated


@pytest.mark.parametrize("record", RECORDS)
def test_record_has_literal_command_and_observed_output(record: str) -> None:
    section = _record_sections()[record]
    fenced = _fenced_lines(section)
    commands = [line for line in fenced if line.startswith("$ ")]
    assert commands, f"{record}: no literal `$ command` line"
    outputs = [line for line in fenced if line.strip() and not line.startswith("$ ")]
    assert outputs, f"{record}: no observed output line"
    assert not any("…" in line for line in fenced), f"{record}: ellipsis inside evidence fence"
    cited_evidence = set(EVIDENCE_CITE_RE.findall(section))
    for name in cited_evidence:
        assert (EVIDENCE / f"{name}.txt").is_file(), f"{record} cites missing evidence/{name}.txt"
    # every command is grounded in the probe: an agy/tmux invocation, a committed fixture or
    # helper path, or a probe-environment path; a heredoc command is grounded by its body
    fixture_names = {str(p.relative_to(AGY_ROOT)) for p in _fixture_files()}
    fixture_names |= {p.name for p in _fixture_files()}
    block = "\n".join(fenced)
    for line in commands:
        command = _command(line)
        scope = block if command.endswith("<<'EOF'") else command
        grounded = GROUNDING_RE.search(scope) or any(name in scope for name in fixture_names)
        assert grounded, f"{record}: command not grounded in the probe: {line[:120]!r}"
        assert not ANNOTATED_COMMAND_RE.search(line), f"{record}: annotated command {line[:120]!r}"


@pytest.mark.parametrize("path", sorted(EVIDENCE.glob("*.txt")), ids=lambda p: p.stem)
def test_evidence_file_commands_are_the_readme_record_commands(path: Path) -> None:
    record = path.name.split("-", 1)[0]
    readme_commands = {
        _command(line)
        for line in _fenced_lines(_record_sections()[record])
        if line.startswith("$ ")
    }
    evidence_commands = {
        _command(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("$ ")
    }
    assert evidence_commands & readme_commands, (
        f"{path.name}: none of its commands appear in README record {record}: "
        f"{sorted(evidence_commands)[:2]}"
    )


def test_cited_helpers_are_committed_and_parse() -> None:
    """Every `bash`/`python3` helper a command cites is a committed probe/ script that parses."""
    cited: dict[str, set[str]] = {}
    sources = [README, *sorted(EVIDENCE.glob("*.txt"))]
    for path in sources:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("$ "):
                continue
            assert not ANNOTATED_COMMAND_RE.search(line), f"{path.name}: annotated command {line!r}"
            for helper in HELPER_CITE_RE.findall(_command(line)):
                cited.setdefault(helper, set()).add(path.name)
    assert cited, "no helper citations found"
    for helper in cited:
        assert helper.startswith("probe/"), (
            f"{helper} cited outside probe/ by {sorted(cited[helper])}"
        )
        assert (AGY_ROOT / helper).is_file(), f"{helper} is not committed ({sorted(cited[helper])})"
    expected = {
        "probe/cancel.sh",
        "probe/net.sh",
        "probe/net-interactive.sh",
        "probe/inputfmt.py",
        "probe/layout.py",
    }
    assert expected <= set(cited), sorted(expected - set(cited))
    for script in sorted((AGY_ROOT / "probe").iterdir()):
        if script.suffix == ".py":
            ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
        elif script.suffix == ".sh":
            subprocess.run(["bash", "-n", str(script)], check=True, timeout=30)


def _record_order(record: str) -> int:
    return int(record.rsplit(".", 1)[1])


LIVE_RECORDS: list[str] = sorted(_live_records(), key=_record_order)


@pytest.mark.parametrize("record", LIVE_RECORDS)
def test_live_record_cites_print_artifact_and_interactive_pane(record: str) -> None:
    section = _record_sections()[record]
    assert any(marker in section for marker in PRINT_ARTIFACTS), (
        f"{record}: no print artifact cited"
    )
    cited = _pane_cites(section)
    assert cited, f"{record}: no pane capture cited"
    existing = {path.stem for path in PANES.glob("*.txt")}
    assert cited <= existing, cited - existing


def test_every_pane_capture_is_cited_and_has_a_real_body() -> None:
    cited: set[str] = set()
    for section in _record_sections().values():
        cited.update(_pane_cites(section))
    panes = sorted(PANES.glob("*.txt"))
    assert panes
    for path in panes:
        body = _pane_body(path)
        assert len(body) >= 2, f"{path.name}: placeholder body (header only)"
        assert path.stem in cited, f"{path.name} is not cited by any record section"
    # the recaptured exit pane shows the real exit state
    exit_body = "\n".join(_pane_body(PANES / "1.1.8-interactive-exit.txt"))
    assert "Pane is dead (status 0" in exit_body and "Resume with -c" in exit_body


def test_evidence_files_are_literal_runs() -> None:
    files = sorted(EVIDENCE.glob("*.txt"))
    assert len(files) >= 40
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("$ "), f"{path.name}: must start with the literal command"
        assert len(text.strip().splitlines()) >= 3, f"{path.name}: no observed output"
        # commands and section markers are ours and never elided; model prose may contain "…"
        for line in text.splitlines():
            if line.startswith("$ ") or line.startswith("--- "):
                assert "…" not in line, f"{path.name}: elided command/marker line {line[:80]!r}"


def test_readme_quoted_stream_lines_exist_verbatim_in_fixtures() -> None:
    corpus = "".join(
        path.read_text(encoding="utf-8")
        for path in _fixture_files()
        if path.suffix in {".txt", ".jsonl", ".json"}
    )
    missing: list[str] = []
    for record, section in _record_sections().items():
        for line in _fenced_lines(section):
            if line.startswith('{"event":') or line.startswith('{"conversation_id":'):
                if line not in corpus:
                    missing.append(f"{record}: {line[:100]}")
    assert not missing, missing


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


def test_response_field_probes_recorded_in_both_modes() -> None:
    """1.1.24: every configured hook answer was delivered in print and interactive mode."""
    rows = [r for r in _jsonl("hook-payloads.jsonl") if r["record"].startswith("1.1.24")]
    answers: dict[str, set[str]] = {mode: set() for mode in MODES}
    for row in rows:
        stdout = row["response"]["stdout"]
        key = json.dumps(stdout, sort_keys=True) if isinstance(stdout, dict) else str(stdout)
        answers[row["mode"]].add(f"{row['event']} {key} exit={row['response']['exit_code']}")
    required = {
        'PreToolUse {"decision": "deny", "reason": "gate0: tool not in allowed set"} exit=0',
        'PreToolUse {"decision": "deny_unless_prior_grant", "reason": "gate0 dupg"} exit=0',
        'PreToolUse {"decision": "allow", "overwrite": {"CommandLine": "echo overwritten-by-hook"}} exit=0',
        'PostInvocation {"terminationBehavior": "terminate"} exit=0',
        'PostInvocation {"terminationBehavior": "force_continue"} exit=0',
        'PreToolUse {"decision": "allow"} exit=1',
        'PreToolUse {"decision": "allow"} exit=2',
        "Stop {} exit=2",
    }
    for mode in MODES:
        assert required <= answers[mode], (mode, required - answers[mode])
        assert any(
            a.startswith('PreToolUse {"decision": "allow", "permissionOverrides"')
            for a in answers[mode]
        ), mode
        assert any(
            a.startswith('PreInvocation {"injectSteps": [{"toolCall"') for a in answers[mode]
        ), mode
        assert any(
            a.startswith('PreInvocation {"injectSteps": [{"userMessage"') for a in answers[mode]
        ), mode
        assert any(a.startswith('Stop {"decision": "continue"') for a in answers[mode]), mode
    # the forced end: eleven Stop hooks (executionNum 0–10) per mode, all answered continue
    for mode in MODES:
        stops = sorted(
            r["payload"]["executionNum"]
            for r in rows
            if r["mode"] == mode
            and r["event"] == "Stop"
            and isinstance(r["response"]["stdout"], dict)
            and r["response"]["stdout"].get("decision") == "continue"
        )
        assert stops == list(range(11)), (mode, stops)


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


def test_isolated_home_models_probe_recorded() -> None:
    captures = json.loads((AGY_ROOT / "command-captures.json").read_text(encoding="utf-8"))
    unauth = [c for c in captures["commands"] if c["record"].startswith("1.1.20 unauthenticated")]
    assert unauth, "isolated-HOME models probe missing"
    for capture in unauth:
        assert "mktemp -d" in capture["command"]
        assert capture["agy_exit_codes"] and set(capture["agy_exit_codes"]) == {1}
        assert "Please sign in" in capture["stderr_tail"]
        assert "oauth" not in capture["stderr_tail"].lower()
    rows = _outcome_table_rows()
    assert "isolated HOME" in rows["1.1.20"][2]
    evidence = (EVIDENCE / "1.1.20-print-models.txt").read_text(encoding="utf-8")
    assert "Error: Please sign in to view available models." in evidence


def test_gate0_capture_hook_removed_after_probe() -> None:
    captures = json.loads((AGY_ROOT / "command-captures.json").read_text(encoding="utf-8"))
    after = [c for c in captures["commands"] if "after gate0-capture removal" in c["record"]]
    assert len(after) >= 3  # first run, receipt run, pass-3 run
    for capture in after:
        names = [h["name"] for h in capture["stdout"]["command"]["data"]["hooks"]]
        assert names == ["gobby"], names


def test_probe_prompt_list_holds_the_fixed_prompts() -> None:
    prompts = _probe_prompts()
    assert len(prompts) == len(set(prompts)), "duplicate probe prompts"
    fixed = {
        "list the files in this directory",
        "run: ls -la",
        "call the gobby list_mcp_servers tool and report the result",
    }
    assert fixed <= set(prompts), sorted(fixed - set(prompts))
    assert not any(BARE_SLASH_COMMAND_RE.match(p) for p in prompts), (
        "slash commands are not prompts"
    )


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: str(p.relative_to(AGY_ROOT)))
def test_fixture_prompts_are_placeholders_or_listed_probe_prompts(path: Path) -> None:
    """Plan §1.1: free-text prompts are `<PROMPT_TEXT>` unless listed under "Probe prompts"."""
    if path.parent == AGY_ROOT / "probe":
        return
    allowed = set(_probe_prompts())
    for number, prompt in _prompt_positions(path):
        if prompt == "<PROMPT_TEXT>" or BARE_SLASH_COMMAND_RE.match(prompt) or prompt == "<prompt>":
            continue
        assert prompt in allowed, f"{path.name}:{number}: unlisted prompt {prompt!r}"


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: str(p.relative_to(AGY_ROOT)))
def test_fixture_is_scrubbed(path: Path) -> None:
    checks = dict(SCRUB_CHECKS)
    if path.parent == AGY_ROOT / "probe":
        # helpers address the live app-data tree at run time; only their captured output is scrubbed
        del checks["non-brain app-data path"]
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for name, pattern in checks.items():
            match = pattern.search(line)
            assert match is None, (
                f"{path.name}:{number}: {name}: {line[max(0, match.start() - 40) : match.end() + 20]!r}"
            )
