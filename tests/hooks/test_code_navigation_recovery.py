"""Regression boundaries for #22213, #22345, and #22413, including merged #22233 generated output."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.code_navigation_recovery import navigation_requires_index
from gobby.hooks.normalization import normalize_tool_fields

pytestmark = pytest.mark.unit

OUTAGE = '{"error":"io","message":"grant lock: Operation not permitted"}\n'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("web/dist/\ncustom-output/\n")
    (root / "custom-output").mkdir()
    (root / "src").mkdir()
    (root / "src/constants.py").write_text("VALUE = 1\n")
    (root / "src/short.py").write_text("value = 1\n" * 5)
    (root / "src/long.py").write_text("value = 1\n" * 60)
    return root


def event(repo: Path, command: str, output: str = "", *, failed: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(repo),
        "project_path": str(repo),
        "tool_output": output,
        "is_error": failed,
    }
    normalize_tool_fields(data)
    return data


def turn(
    repo: Path,
    command: str,
    output: str = "",
    *,
    failed: bool = False,
    reported: bool = True,
) -> dict[str, Any]:
    """Tracker state after one gcode call: attempt before it, outcome after if reported."""
    before = event(repo, command)
    after = event(repo, command, output, failed=failed) if reported else {}
    return {
        "code_index_attempts": before["canonical_code_index_attempts"],
        "code_index_verified": after.get("canonical_code_index_verified", []),
        "code_index_recoveries": after.get("canonical_code_index_recovery", []),
    }


def read_event(repo: Path, path: str, *, limit: int | None = None) -> dict[str, Any]:
    """A normalized Read tool call; without ``limit`` the scope is the whole file."""
    tool_input: dict[str, Any] = {"file_path": str(repo / path)}
    if limit is not None:
        tool_input["limit"] = limit
    data: dict[str, Any] = {
        "tool_name": "Read",
        "tool_input": tool_input,
        "cwd": str(repo),
        "project_path": str(repo),
    }
    normalize_tool_fields(data)
    return data


def broad_read_blocked(repo: Path, command: str) -> bool:
    """The prefer-gcode gate for broad reads after the code-index reference loads."""
    return navigation_requires_index(event(repo, command), {}, "read", broad_only=True)


def test_typed_outage_opens_the_checkout_for_every_operation(repo: Path) -> None:
    variables = turn(
        repo,
        "git status --short; gcode grep VALUE src; gcode outline src/constants.py",
        " M src/constants.py\n" + OUTAGE * 2,
        failed=True,
    )
    assert [record["action"] for record in variables["code_index_recoveries"]] == ["outage"]
    assert not navigation_requires_index(event(repo, "cat src/untouched.py"), variables)
    assert not navigation_requires_index(event(repo, "rg VALUE ."), variables)


def test_external_project_outage_does_not_open_current_checkout(repo: Path) -> None:
    variables = turn(repo, "gcode --project /external/archive grep VALUE src", OUTAGE, failed=True)
    assert variables["code_index_recoveries"] == [
        {"action": "outage", "paths": ["/external/archive"]}
    ]
    assert navigation_requires_index(event(repo, "rg VALUE src"), variables)


@pytest.mark.parametrize(
    ("command", "opens"),
    [
        ("gcode outline src/constants.py 2>&1 | head -40", True),
        ("echo '---'; gcode outline src/constants.py", True),
        ("cat notes.txt; gcode outline src/constants.py", False),
        ("head -n 5 notes.txt; gcode outline src/constants.py", False),
        ("gcode outline src/constants.py | tail -n +2", True),
        ('echo "$(cat notes.txt)"; gcode outline src/constants.py', False),
        ("gcode outline src/constants.py | python3 -c 'print(1)'", False),
    ],
)
def test_outage_requires_output_only_gcode_can_print(repo: Path, command: str, opens: bool) -> None:
    result = event(repo, command, OUTAGE, failed=True)
    actions = [record["action"] for record in result["canonical_code_index_recovery"]]
    assert ("outage" in actions) is opens


@pytest.mark.parametrize("output", ["", '{"error":"invalid_query","message":"bad pattern"}'])
def test_failed_attempt_opens_only_its_scope(repo: Path, output: str) -> None:
    variables = turn(repo, "gcode grep 'query/with.dot' src", output, failed=True)
    assert variables["code_index_recoveries"] == variables["code_index_attempts"]
    assert not navigation_requires_index(event(repo, "rg VALUE src/constants.py"), variables)
    assert navigation_requires_index(event(repo, "rg VALUE ."), variables)
    assert navigation_requires_index(event(repo, "cat src/constants.py"), variables)


def test_unreported_outcome_keeps_the_attempt_open(repo: Path) -> None:
    variables = turn(repo, "gcode outline src/constants.py", reported=False)
    assert not navigation_requires_index(event(repo, "cat src/constants.py"), variables)
    assert navigation_requires_index(event(repo, "cat src/other.py"), variables)
    assert navigation_requires_index(event(repo, "rg VALUE src/constants.py"), variables)


def test_verified_output_closes_only_its_own_attempt(repo: Path) -> None:
    verified = turn(repo, "gcode outline src/constants.py", "src/constants.py:1 [constant] VALUE")
    assert verified["code_index_verified"] == verified["code_index_attempts"]
    assert navigation_requires_index(event(repo, "cat src/constants.py"), verified)

    retried = turn(repo, "gcode outline src/constants.py", reported=False)
    merged = {key: verified[key] + retried[key] for key in verified}
    assert not navigation_requires_index(event(repo, "cat src/constants.py"), merged)


@pytest.mark.parametrize(
    ("command", "redirected"),
    [
        ("cat src/constants.py", False),
        ("cat src/long.py", True),
        ("sed -n '1,80p' src/constants.py", True),
        ("head -n 40 src/constants.py", False),
        ("rg VALUE src", False),
    ],
)
def test_source_read_redirect_skips_narrow_reads_and_other_operations(
    repo: Path, command: str, redirected: bool
) -> None:
    variables = {"code_index_navigation_used_this_turn": True}
    result = navigation_requires_index(event(repo, command), variables, "read", broad_only=True)
    assert result is redirected


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("cat src/constants.py", False),
        ("cat src/short.py", False),
        ("cat src/constants.py src/short.py", False),
        ("nl -ba src/short.py", False),
        ("nl -ba src/long.py", True),
        ("cat src/long.py", True),
        ("cat src/short.py src/long.py", True),
        ("cat src/missing.py", True),
    ],
)
def test_full_file_reads_follow_the_verified_line_count(
    repo: Path, command: str, blocked: bool
) -> None:
    assert broad_read_blocked(repo, command) is blocked


@pytest.mark.parametrize(
    ("lines", "trailing_newline", "blocked"),
    [
        (40, True, False),
        (41, True, True),
        (40, False, False),
        (41, False, True),
    ],
)
def test_whole_file_reads_stop_at_the_narrow_line_ceiling(
    repo: Path, lines: int, trailing_newline: bool, blocked: bool
) -> None:
    """A trailing partial line counts, so the ceiling holds without a final newline."""
    body = "value = 1\n" * (lines - 1) + ("value = 1\n" if trailing_newline else "value = 1")
    (repo / "src/boundary.py").write_text(body)
    assert broad_read_blocked(repo, "cat src/boundary.py") is blocked


def test_whole_file_reads_stay_blocked_past_the_scan_limit(repo: Path) -> None:
    """A one-line file too large to scan cheaply stays unverified, so it stays broad."""
    (repo / "src/minified.py").write_text("x = 1;" * 50_000 + "\n")
    assert broad_read_blocked(repo, "cat src/minified.py")


def test_read_tool_without_limit_follows_the_file_size(repo: Path) -> None:
    variables = {"code_index_navigation_used_this_turn": True}
    assert not navigation_requires_index(
        read_event(repo, "src/short.py"), variables, "read", broad_only=True
    )
    assert not navigation_requires_index(
        read_event(repo, "src/long.py", limit=30), variables, "read", broad_only=True
    )
    assert navigation_requires_index(
        read_event(repo, "src/long.py"), variables, "read", broad_only=True
    )


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("head -c 500 src/long.py", False),
        ("tail -c 2000 src/long.py", False),
        ("head -c 4096 src/long.py", False),
        ("head -c 4097 src/long.py", True),
        ("tail -c 5000 src/long.py", True),
        ("tail -c +1 src/long.py", True),
    ],
)
def test_byte_bounded_reads_exempt_only_the_verified_window(
    repo: Path, command: str, blocked: bool
) -> None:
    assert broad_read_blocked(repo, command) is blocked


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("cat src/long.py > gen.py", False),
        ("cat src/long.py src/constants.py > gen.py", False),
        ("cat src/long.py >> gen.py", False),
        ("cat src/long.py | tee gen.py", True),
        ("cat src/long.py 2> err.txt", True),
    ],
)
def test_redirected_copies_never_reach_the_model(repo: Path, command: str, blocked: bool) -> None:
    assert broad_read_blocked(repo, command) is blocked


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("sed -n 94,163p src/long.py | head -40", False),
        ("nl -ba src/long.py | sed -n '10,30p'", False),
        ("cat src/long.py | head -40", False),
        ("sed '1,40!d' src/long.py", False),
        ("cat src/long.py | head -60", True),
        ("sed '1,80!d' src/long.py", True),
        ("cat src/long.py | head -40 | wc -l", True),
    ],
)
def test_pipeline_final_stage_bounds_output(repo: Path, command: str, blocked: bool) -> None:
    assert broad_read_blocked(repo, command) is blocked


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("cat third_party/lib/x.py", False),
        ("rg pattern third_party/lib", False),
        ("rg pattern src/third_party/lib", False),
        ("rg pattern src", True),
        ("cat src/long.py", True),
    ],
)
def test_committed_third_party_reads_like_vendor(repo: Path, command: str, blocked: bool) -> None:
    assert navigation_requires_index(event(repo, command), {}) is blocked


@pytest.mark.parametrize(
    "command",
    [
        'sed -n "$(grep -n VALUE src/long.py | cut -d: -f1)p" src/long.py',
        'sed -n "${start},${end}p" src/long.py',
        'cat "$SCRATCH/probe.py"',
    ],
)
def test_unverifiable_bounds_and_variable_paths_stay_blocked(repo: Path, command: str) -> None:
    assert broad_read_blocked(repo, command)


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("rg pattern web/dist/assets", False),
        ("rg pattern custom-output", False),
        ("rg pattern node_modules/package", False),
        ("rg pattern vendor/lib", False),
        ("rg pattern dist/assets", False),
        ("rg pattern src/build", True),
        ("rg pattern web/dist/assets src", True),
        ("rg pattern web/dist/assets; rg pattern src", True),
        ("rg pattern src; rg pattern web/dist/assets", True),
        ("head -n 40 src/constants.py", True),
        ("head -n 40 src/constants.py; cat src/other.py", True),
        ("gcode outline src/constants.py; cat src/other.py", True),
        ("rg pattern /var/log/system.log", False),
        ("rg pattern /var/log/system.log src", True),
    ],
)
def test_target_exemptions(repo: Path, command: str, blocked: bool) -> None:
    assert navigation_requires_index(event(repo, command), {}) is blocked


@pytest.mark.parametrize(
    "output",
    [
        "file has no indexed symbols in current project: src/constants.py",
        "file type has no AST parser support; `gcode outline` is AST-only: src/constants.py",
        "file not indexed in current project: src/constants.py",
    ],
)
def test_zero_symbol_read_is_file_and_operation_scoped(repo: Path, output: str) -> None:
    result = event(repo, "gcode outline src/constants.py", output)
    variables = {"code_index_recoveries": result["canonical_code_index_recovery"]}
    assert not navigation_requires_index(event(repo, "cat src/constants.py"), variables)
    assert navigation_requires_index(event(repo, "cat src/other.py"), variables)
    assert navigation_requires_index(event(repo, "rg VALUE src/constants.py"), variables)
    assert navigation_requires_index(event(repo, "cat src/constants.py src/other.py"), variables)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "No results.",
        "warning: gcode index refresh already running; reading existing index\nsrc/constants.py:1:VALUE",
    ],
)
def test_search_miss_and_usable_refresh_do_not_grant_recovery(repo: Path, output: str) -> None:
    result = event(repo, "gcode grep VALUE src", output)
    assert not result["canonical_code_index_recovery"]
    assert navigation_requires_index(event(repo, "rg VALUE src"), {})


def test_same_turn_write_exemption_is_per_segment(repo: Path) -> None:
    variables = {"turn_written_paths": [str(repo / "src/constants.py")]}
    assert not navigation_requires_index(event(repo, "cat src/constants.py"), variables)
    assert navigation_requires_index(
        event(repo, "cat src/constants.py; cat src/other.py"), variables
    )


def test_ignored_allowlisted_targets_remain_indexed(repo: Path) -> None:
    (repo / ".gobby").mkdir()
    (repo / ".gobby/gcode.json").write_text(
        '{"index":{"hidden_allowlist":["custom-output/**/*.py"]}}'
    )
    assert navigation_requires_index(event(repo, "rg VALUE custom-output"), {})


def test_outline_signature_cannot_forge_empty_diagnostic(repo: Path) -> None:
    result = event(
        repo,
        "gcode outline src/constants.py",
        'src/constants.py:1-3 [function] f sig=def f(x="file has no indexed symbols in current project:")',
    )
    assert not result["canonical_code_index_recovery"]


def test_linked_checkout_generated_paths_remain_accessible(repo: Path) -> None:
    linked = repo.parent / "linked"
    linked.mkdir()
    gitdir = repo / ".git/worktrees/linked"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    (linked / ".git").write_text(f"gitdir: {gitdir}\n")
    assert not navigation_requires_index(event(repo, f"rg VALUE {linked}/dist/assets"), {})
    assert navigation_requires_index(event(repo, f"rg VALUE {linked}/src"), {})
