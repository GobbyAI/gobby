"""Literal operands retain their scope without treating scripts or data as paths."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.hooks.provider_launch_guard import blocks_direct_provider_launch
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_rule_file

pytestmark = pytest.mark.unit
SESSION = "11111111-1111-4111-8111-111111111111"


def _metadata(command: str, root: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(root)},
        "project_path": str(root),
    }
    normalize_tool_fields(data)
    return data


@pytest.mark.parametrize("nested", [False, True])
def test_redirected_index_update_has_literal_repository_scope(tmp_path: Path, nested: bool) -> None:
    command = "git add -A > out.txt"
    if nested:
        command = f"bash -c '{command}'"
    bare = _metadata("git add -A", tmp_path)
    redirected = _metadata(command, tmp_path)
    assert redirected["canonical_repo_mutation"] == bare["canonical_repo_mutation"] is True
    assert redirected["canonical_file_paths"] == ["out.txt"]
    assert not redirected.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize(
    "command,paths",
    [
        ("git rm a", ["a"]),
        ("git rm -rf -- a b.py", ["a", "b.py"]),
        ("git mv a b", ["a", "b"]),
        ("git -C sub mv -f -- a b.py", ["sub/a", "sub/b.py"]),
        ('git rm "$@"', []),
    ],
)
def test_git_remove_and_move_scope(tmp_path: Path, command: str, paths: list[str]) -> None:
    data = _metadata(command, tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    if paths:
        assert data["canonical_file_paths"] == [str(tmp_path / path) for path in paths]
        assert not data.get("canonical_repo_mutation_scope_unknown")
    else:
        assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "command,paths,unknown",
    [
        ("pushd sub; touch y.py", ["sub/y.py"], False),
        ("pushd sub; popd; touch y.py", ["y.py"], False),
        ("pushd sub; pushd deep; popd; touch y.py", ["sub/y.py"], False),
        ("pushd sub; cd other; popd; touch y.py", ["y.py"], False),
        ("pushd sub | cat; touch y.py", ["y.py"], False),
        ("pushd sub & touch y.py", ["y.py"], False),
        ("pushd sub; { pushd deep; } | cat; popd; touch y.py", ["y.py"], False),
        ("if true; then pushd sub; fi; popd; touch y.py", [], True),
        ("pushd sub; eval 'pushd other'; popd; touch y.py", [], True),
        ("popd; touch y.py", [], True),
        ("pushd +1; touch y.py", [], True),
        ("pushd sub; dirs -c; popd; touch y.py", [], True),
        ("pushd sub; eval 'dirs -c'; popd; touch y.py", [], True),
        ("pushd sub; popd | cat; touch y.py", ["sub/y.py"], False),
        ("pushd sub; pushd deep; pushd; touch y.py", ["sub/y.py"], False),
    ],
)
def test_directory_stack_scope(
    tmp_path: Path, command: str, paths: list[str], unknown: bool
) -> None:
    data = _metadata(command, tmp_path)
    assert data.get("canonical_file_paths", []) == paths
    assert bool(data.get("canonical_repo_mutation_scope_unknown")) is unknown


@pytest.mark.parametrize(
    "options",
    ["'s/a/b/'", "-e 's/a/b/'", "-es/a/b/", "-ne 's/a/b/'", "--expression=s/a/b/"],
)
def test_sed_script_is_not_a_file_operand(tmp_path: Path, options: str) -> None:
    data = _metadata(f"sed {options} /tmp/scratch/in.py > /tmp/scratch/out.py", tmp_path)
    assert data["canonical_file_paths"] == ["/tmp/scratch/in.py", "/tmp/scratch/out.py"]
    assert data["canonical_write_file_paths"] == ["/tmp/scratch/out.py"]
    assert data["canonical_repo_mutation"] is False


@pytest.mark.parametrize("option", ["-f script.sed", "-fscript.sed", "--file=script.sed"])
def test_sed_file_script_retains_read_scope(tmp_path: Path, option: str) -> None:
    data = _metadata(f"sed {option} in.py", tmp_path)
    assert data["canonical_file_paths"] == ["script.sed", "in.py"]
    assert data["canonical_tool_kind"] == "read"


def test_sed_in_place_tracks_every_input(tmp_path: Path) -> None:
    data = _metadata("sed -i -e 's/a/b/' first.py second.py", tmp_path)
    assert data["canonical_file_paths"] == ["first.py", "second.py"]
    assert data["canonical_write_file_paths"] == ["first.py", "second.py"]


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("payload", ["the ` char", "the $( char", "$(echo ok) then `"])
def test_malformed_data_heredoc_does_not_make_a_path_a_launch(
    tmp_path: Path, provider: str, payload: str
) -> None:
    scratch = tmp_path / f"{provider}-501" / "first_ran"
    command = f"touch {scratch} && cat <<EOF\n{payload}\nEOF"
    assert not blocks_direct_provider_launch("Bash", {"command": command})
    encoded = (
        command.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("`", "\\x60")
    )
    assert not blocks_direct_provider_launch("Bash", {"command": f"bash -c $'{encoded}'"})


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_launches_remain_blocked_next_to_malformed_data(provider: str) -> None:
    assert blocks_direct_provider_launch(
        "Bash", {"command": f"{provider} exec hi; cat <<EOF\nthe ` char\nEOF"}
    )
    assert blocks_direct_provider_launch(
        "Bash", {"command": f"cat <<EOF\n$({provider} exec hi) then `\nEOF"}
    )
    assert blocks_direct_provider_launch(
        "Bash", {"command": f"sh <<EOF\n{provider} exec hi\nthe ` char\nEOF"}
    )


@pytest.fixture
def scope_engine(hub_db: HubDatabase) -> RuleEngine:
    for relative in (
        "task-enforcement/block-unresolved-scope-shell-write.yaml",
        "task-enforcement/require-task-before-edit.yaml",
        "skill-discovery/require-python-skill.yaml",
    ):
        result = sync_rule_file(hub_db, get_bundled_rules_path() / relative, tag="gobby")
        assert result["success"] is True
    hub_db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    for name in ("require-task-before-edit", "require-python-skill"):
        row = RuleDefinitionManager(hub_db).get_by_name(name)
        assert row is not None and row.enabled
    return RuleEngine(hub_db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,blocked",
    [
        ("git add -A > out.txt", True),
        ("git add -A > /tmp/scratch/out.txt", True),
        ("sed 's/a/b/' /tmp/scratch/in.py > /tmp/scratch/out.py", False),
        ("sed -e 's/a/b/' /tmp/scratch/in.py > /tmp/scratch/out.py", False),
        ("sed 's/a/b/' in.py > out.py", True),
        ('sed "$PROGRAM" /tmp/scratch/in.py > /tmp/scratch/out.py', True),
        ('sed -e "$PROGRAM" /tmp/scratch/in.py > /tmp/scratch/out.py', True),
    ],
)
async def test_literal_write_scope_enforcement_without_claim(
    tmp_path: Path, scope_engine: RuleEngine, command: str, blocked: bool
) -> None:
    data = _metadata(command, tmp_path)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
    )
    result = await scope_engine.evaluate(
        event,
        session_id=SESSION,
        variables={"require_task_before_edit": True, "task_claimed": False, "loaded_skills": []},
    )
    assert result.decision == ("block" if blocked else "allow"), result.reason


@pytest.mark.parametrize("operation", ["rm", "mv"])
@pytest.mark.parametrize("flag", ["-n", "--dry-run"])
def test_git_dry_run_is_read_only(tmp_path: Path, operation: str, flag: str) -> None:
    data = _metadata(f"git {operation} {flag} a b", tmp_path)
    assert data["canonical_tool_kind"] == "execute"
    assert not data.get("canonical_repo_mutation")


@pytest.mark.parametrize("program", ['"$PROGRAM"', '-e "$PROGRAM"', '--expression="$PROGRAM"'])
def test_unexpanded_sed_program_cannot_prove_scratch_only_scope(
    tmp_path: Path, program: str
) -> None:
    data = _metadata(f"sed {program} /tmp/scratch/in.py > /tmp/scratch/out.py", tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "program,path",
    [
        ("-n '$p'", "/tmp/scratch/in.py"),
        ("'$d'", "in.py"),
        ("-n '10,$p'", "src/x.py"),
        ("-n '/start/,$p'", "/tmp/scratch/in.py"),
        ("'N;$!P;$!D;$d'", "src/x.py"),
        ("-n -e '$p'", "src/x.py"),
        ("--expression='$p'", "src/x.py"),
    ],
)
def test_single_quoted_sed_addresses_are_literal(tmp_path: Path, program: str, path: str) -> None:
    data = _metadata(f"sed {program} {path}", tmp_path)
    assert data["canonical_tool_kind"] == "read"
    assert data["canonical_file_paths"] == [path]
    assert not data.get("canonical_repo_mutation")


@pytest.mark.parametrize("suffix,path", [(".bak ", "/tmp/scratch/in.py"), ("", "src/x.py")])
def test_sed_backup_suffix_is_not_an_input(tmp_path: Path, suffix: str, path: str) -> None:
    data = _metadata(f"sed -i {suffix}'s/a/b/' {path}", tmp_path)
    assert data["canonical_file_paths"] == [path]
    assert data["canonical_write_file_paths"] == [path]
    assert data["canonical_repo_mutation"] is (not path.startswith("/tmp/"))


@pytest.mark.parametrize("payload", ["`claude -p hi", "$(claude -p hi", "`\n$(claude -p hi)"])
def test_incomplete_data_substitution_still_inspects_provider_launch(payload: str) -> None:
    command = f"cat <<EOF\n{payload}\nEOF"
    assert blocks_direct_provider_launch("Bash", {"command": command})
