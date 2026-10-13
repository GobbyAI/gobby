"""Literal operands retain their scope without treating scripts or data as paths."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks import _normalization_canonical, _normalization_compound
from gobby.hooks._normalization_shell import ShellScan, scan_shell_command
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.hooks.provider_launch_guard import blocks_direct_provider_launch
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_rule_file

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "command",
    [
        "cat <\\\n(touch a.py)",
        "cat 2<\\\n(touch a.py)",
        "cat >\\\n(touch a.py)",
        "echo $\\\n(touch a.py)",
        'echo "$\\\n(touch a.py)"',
        "cat <(touch a.py)\\\n",
        "cat <\\\n\\\n(touch a.py)\\\n\\\n",
        "x=$\\\n(touch a.py)",
        "echo $(echo $\\\n(touch a.py))",
        "echo `echo $\\\n(touch a.py)`",
        "cat <(cat <\\\n(touch a.py))",
    ],
)
def test_split_substitution_opener_reports_write(tmp_path: Path, command: str) -> None:
    metadata = _metadata(command, tmp_path)
    control = _metadata(command.replace("\\\n", ""), tmp_path)
    assert control["canonical_repo_mutation"] is True
    assert control["canonical_file_paths"] == ["a.py"]
    assert metadata["canonical_repo_mutation"] is True
    assert metadata["canonical_file_paths"] == control["canonical_file_paths"]


def test_unclosed_split_substitution_opener_fails_closed(tmp_path: Path) -> None:
    metadata = _metadata("echo $\\\n(", tmp_path)
    assert metadata["canonical_repo_mutation"] is True


@pytest.mark.parametrize(
    "command",
    [
        "case x in <\\\n(touch a.py)) ;; esac",
        "case x in y|<(touch a.py)) ;; esac",
        "case x in <(touch a.py)\\\n) ;; esac",
        "cat <(touch a.py)x",
        'cat <(touch a.py)"x"',
    ],
)
def test_process_substitution_with_suffix_reports_write(tmp_path: Path, command: str) -> None:
    metadata = _metadata(command, tmp_path)
    assert metadata["canonical_repo_mutation"] is True
    assert metadata["canonical_tool_kind"] == "write"


@pytest.mark.parametrize(
    "command",
    [
        "echo '$\\\n(touch a.py)'",
        'echo "\\$\\\n(touch a.py)"',
        'echo "<\\\n(touch a.py)"',
        'echo ">\\\n(touch a.py)"',
    ],
)
def test_literal_split_substitution_stays_read_only(tmp_path: Path, command: str) -> None:
    assert _metadata(command, tmp_path).get("canonical_repo_mutation") is not True


@pytest.mark.parametrize("delimiter", ["EOF", "'EOF'", '"EOF"'])
def test_heredoc_split_substitution_write(tmp_path: Path, delimiter: str) -> None:
    command = f"cat <<{delimiter}\n$\\\n(touch a.py)\nEOF"
    metadata = _metadata(command, tmp_path)
    if delimiter == "EOF":
        assert metadata["canonical_repo_mutation"] is True
        assert metadata["canonical_file_paths"] == ["a.py"]
    else:
        assert metadata.get("canonical_repo_mutation") is not True


SESSION = "11111111-1111-4111-8111-111111111111"


def _metadata(command: str, root: Path, cwd: Path | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(cwd or root)},
        "project_path": str(root),
    }
    normalize_tool_fields(data)
    return data


@pytest.mark.parametrize("prefix", ["", "command ", "env LC_ALL=C "])
@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("sort -o out.txt in.txt", "out.txt"),
        ('"sort" -o out.txt in.txt', "out.txt"),
        ('sort -o "out file.txt" "in file.txt"', "out file.txt"),
        ("sort --output=out.txt in.txt", "out.txt"),
        ("sort --out=out.txt in.txt", "out.txt"),
        ("sort --out out.txt in.txt", "out.txt"),
        ("sort --outp out.txt in.txt", "out.txt"),
        ("sort --o=out.txt in.txt", "out.txt"),
        ("sort -oout.txt in.txt", "out.txt"),
        ("sort -ro out.txt -k 1 in.txt", "out.txt"),
        ("ln -s a b", "b"),
        ('"ln" -s a b', "b"),
        ('ln -s "source file" "link file"', "link file"),
        ("ln -st links a", "links/a"),
        ("tar -xf x.tar -C dir", "dir"),
        ('"tar" -xf x.tar -C dir', "dir"),
        ('tar -xf "x file.tar" -C "output dir"', "output dir"),
        ("tar xf x.tar -Cdir", "dir"),
        ("tar xfC x.tar dir", "dir"),
        ("tar --extract --file=x.tar --directory=dir", "dir"),
        ("tar --extr --file=x.tar --dire=dir", "dir"),
        ("tar --ext -f x.tar --dire=dir", "dir"),
        ("tar -xf x.tar --cd dir", "dir"),
        ("tar -xf x.tar --cd=dir", "dir"),
        ("tar --extract -f x.tar --dir dir", "dir"),
        ("tar -xvzf x.tar -C dir", "dir"),
        ("tar --extract --verbose --file=x.tar --directory=dir", "dir"),
        ("tar -xf x.tar", "."),
    ],
)
def test_file_writer_options_report_destination(
    tmp_path: Path, prefix: str, command: str, target: str
) -> None:
    data = _metadata(f"cd '{tmp_path}' && {prefix}{command}", tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert str(tmp_path / target) in data["canonical_file_paths"]


@pytest.mark.parametrize(
    "command",
    [
        "sort in.txt",
        "sort -k 'o' in.txt",
        "sort -- in-o.txt",
        "sort --key o in.txt",
        "sort --random-source o in.txt",
        "sort --compress-program o in.txt",
        "tar -tf x.tar -C dir",
        "tar tf extract.tar -C dir",
        "tar --list --file=x.tar --directory=dir",
        "tar -f x.tar member -t -C dir",
        "tar -f x.tar member -x -O -C dir",
        "tar -xOf x.tar",
        "tar -xf x.tar --to-stdout",
        "tar --extract --file=x.tar --to-std",
        "git -C sub status",
        'git -C "sub directory" status',
    ],
)
def test_file_writer_read_only_forms(tmp_path: Path, command: str) -> None:
    assert not _metadata(command, tmp_path).get("canonical_repo_mutation", False)


@pytest.mark.parametrize("directory", ["sub", '"sub directory"'])
def test_git_directory_selector_matches_call_directory(tmp_path: Path, directory: str) -> None:
    selected = _metadata(f"cd '{tmp_path}' && git -C {directory} add -A", tmp_path)
    direct = _metadata(f"cd '{tmp_path}' && cd {directory} && git add -A", tmp_path)
    assert selected["canonical_repo_mutation"] is True
    assert selected["canonical_repo_mutation"] == direct["canonical_repo_mutation"]
    assert selected.get("canonical_file_paths") == direct.get("canonical_file_paths")
    assert selected.get("canonical_repo_mutation_scope_unknown") == direct.get(
        "canonical_repo_mutation_scope_unknown"
    )


@pytest.mark.parametrize("command", ["sort -o out.txt in.txt", "ln -s a b", "tar -xf x.tar -C dir"])
def test_shell_inline_file_writer(tmp_path: Path, command: str) -> None:
    assert _metadata(f"sh -c '{command}'", tmp_path)["canonical_repo_mutation"] is True


@pytest.mark.parametrize(
    "command",
    ["sort -o /tmp/out.txt in.txt", "ln -s a /tmp/link", "tar -xf x.tar -C /tmp/output"],
)
def test_file_writer_external_destination(tmp_path: Path, command: str) -> None:
    data = _metadata(f"cd '{tmp_path}' && {command}", tmp_path)
    assert not data.get("canonical_repo_mutation", False)
    assert data["canonical_tool_kind"] == "write"


@pytest.mark.parametrize(
    "command", ['sort -o "$OUT" in.txt', 'ln -s a "$LINK"', 'tar -xf x.tar -C "$DIR"']
)
def test_file_writer_dynamic_destination_fails_closed(tmp_path: Path, command: str) -> None:
    data = _metadata(command, tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    ("command", "directory"),
    [
        ("tar -xf x.tar a -C /tmp/extract b", "/tmp/extract"),
        ("tar xf x.tar a -C /tmp/extract b", "/tmp/extract"),
        ("tar --extract --file=x.tar a -C /tmp/extract b", "/tmp/extract"),
        ("tar -f x.tar member -x -C /tmp/out", "/tmp/out"),
        ('tar -f "x file.tar" "member file" --extract -C "/tmp/output dir"', "/tmp/output dir"),
    ],
)
def test_tar_members_before_directory_switch_keep_both_scopes(
    tmp_path: Path, command: str, directory: str
) -> None:
    data = _metadata(f"cd '{tmp_path}' && {command}", tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert set(data["canonical_file_paths"]) == {str(tmp_path), directory}


@pytest.mark.parametrize("option", ["--dire", "--cd"])
def test_tar_directory_alias_from_external_cwd(tmp_path: Path, option: str) -> None:
    data = _metadata(f"cd /tmp && tar -xf x.tar {option}='{tmp_path}/sub'", tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert str(tmp_path / "sub") in data["canonical_file_paths"]


@pytest.mark.parametrize(
    "command",
    [
        "tar -xPf x.tar",
        "tar xPf x.tar",
        "tar -x --absolute-names -f x.tar",
        "tar -x --absolute-paths -f x.tar",
        "tar -x --ab -f x.tar",
        "tar -x --abs -f x.tar",
        "tar -x --absolute- -f x.tar",
        "tar -x --insecure -f x.tar",
        "tar -x --insec -f x.tar",
    ],
)
def test_tar_absolute_members_fail_closed(tmp_path: Path, command: str) -> None:
    data = _metadata(f"cd /tmp && {command}", tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("external_cwd", [False, True])
@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("tar -xf x.tar --exclude-from -O -C ROOT", "."),
        ("tar -xf x.tar -X -O -C ROOT", "."),
        ("tar -x -X -O -f x.tar -C ROOT", "."),
        ("tar -x -I -O -f x.tar -C ROOT/sub", "sub"),
        ("tar -xf x.tar --gname -O", None),
        ("tar -xf x.tar --uname -O", None),
        ("tar -xf x.tar --passphrase -O", None),
        ("tar -xf x.tar --gname -C /tmp member.txt", None),
        ("tar -xf x.tar --to-command=cat", None),
        ("tar -x -H -f x.tar", None),
    ],
)
def test_tar_option_values_and_unmodelled_options_preserve_write_guard(
    tmp_path: Path, external_cwd: bool, command: str, target: str | None
) -> None:
    cwd = "/tmp" if external_cwd else str(tmp_path)
    command = command.replace("ROOT", f"'{tmp_path}'")
    data = _metadata(f"cd '{cwd}' && {command}", tmp_path)
    assert data["canonical_repo_mutation"] is True
    if target is None:
        assert data["canonical_repo_mutation_scope_unknown"] is True
    else:
        assert str(tmp_path / target) in data["canonical_file_paths"]
        assert not data.get("canonical_repo_mutation_scope_unknown", False)


@pytest.mark.parametrize(
    "command",
    [
        "f() (touch a.py); f",
        "f() (case x in x) touch a.py;; esac); f",
        "f () { touch a.py; }; f",
        "function f() (touch a.py); f",
        "if true; then f() (touch a.py); f; fi",
        "f() { g() (touch a.py); g; }; f",
        "f(){ touch a.py; }; f",
        "f()(touch a.py); f",
        "if f() (touch a.py); f; then :; fi",
        "while f() (touch a.py); f; do break; done",
        "case x in x) f() (touch a.py); f;; esac",
        "! f() (touch a.py); f",
        "f() while touch a.py; do break; done; f",
        "f() if touch a.py; then :; fi; f",
        "f() until touch a.py; do break; done; f",
        "f() (touch a.py); f; cd ..",
        "a.b() (touch a.py); a.b",
        "my-func() (touch a.py); my-func",
    ],
)
def test_invoked_function_body_reports_write(tmp_path: Path, command: str) -> None:
    metadata = _metadata(command, tmp_path)
    assert metadata["canonical_repo_mutation"] is True
    assert metadata["canonical_tool_kind"] == "write"


@pytest.mark.parametrize(
    "command",
    [
        "printf '%s' 'f() { touch a.py; }; f'",
        "f() { printf '%s' 'touch a.py'; }; f",
        "cat <<'EOF'\nf() { touch a.py; }; f\nEOF",
    ],
)
def test_function_body_literal_does_not_report_write(tmp_path: Path, command: str) -> None:
    metadata = _metadata(command, tmp_path)
    assert not metadata.get("canonical_repo_mutation")


def test_function_called_after_directory_change_has_conservative_scope(tmp_path: Path) -> None:
    command = f"cd ..; f() (touch a.py); cd '{tmp_path}'; f"
    metadata = _metadata(command, tmp_path)
    assert metadata["canonical_repo_mutation"] is True


@pytest.mark.parametrize("loader", ["eval", "source", "."])
@pytest.mark.parametrize("body", ["{ touch a.py; }", "(touch a.py)"])
def test_indirect_function_uses_call_directory(tmp_path: Path, loader: str, body: str) -> None:
    root = tmp_path / "repo"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    definition = f"f() {body}"
    script = scratch / "functions.sh"
    script.write_text(definition)
    setup = f"eval '{definition}'" if loader == "eval" else f"{loader} '{script}'"
    command = f"cd '{scratch}'; {setup}; cd '{root}'; f"

    metadata = _metadata(command, root)

    assert metadata["canonical_repo_mutation"] is True
    assert metadata.get("canonical_repo_mutation_scope_unknown") is True


@pytest.mark.parametrize("loop", ["select d in x", "select d"])
def test_select_directory_change_leaves_followup_scope_unknown(tmp_path: Path, loop: str) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    command = f"{loop}; do cd '{scratch}'; break; done; touch a.py"

    metadata = _metadata(command, tmp_path)

    assert metadata["canonical_repo_mutation"] is True
    assert metadata.get("canonical_repo_mutation_scope_unknown") is True


@pytest.mark.parametrize(
    "command",
    [
        "eval 'touch a.py'",
        "eval 'f() { printf ready; }'; f",
        "select d in x; do printf ready; break; done; touch a.py",
    ],
)
def test_indirect_and_select_controls_keep_external_scope(tmp_path: Path, command: str) -> None:
    root = tmp_path / "repo"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()

    metadata = _metadata(command, root, cwd=scratch)

    assert not metadata.get("canonical_repo_mutation")
    assert not metadata.get("canonical_repo_mutation_scope_unknown")


def test_function_definition_retains_known_external_scope(tmp_path: Path) -> None:
    metadata = _metadata("cd ..; f() (touch a.py); f", tmp_path)
    assert metadata["canonical_tool_kind"] == "write"
    assert metadata["canonical_repo_mutation"] is False


@pytest.mark.timeout(5)
@pytest.mark.parametrize("definition_count", [40, 300])
def test_many_function_definitions_report_write(tmp_path: Path, definition_count: int) -> None:
    definitions = [f"f{i}() {{ echo {i}; }}" for i in range(definition_count - 1)]
    last = f"f{definition_count - 1}"
    command = "; ".join([*definitions, f"{last}() {{ touch a.py; }}", last])
    assert _metadata(command, tmp_path)["canonical_repo_mutation"] is True


@pytest.mark.parametrize(
    "command",
    [
        "case b in a) f() { :; };; b) g() ( touch a.py );; esac; g",
        "case b in a) f() { :; };; b) g() if touch a.py; then :; fi;; esac; g",
        "f() { cd /tmp; }; touch a.py",
    ],
)
def test_function_compound_context_reports_write(tmp_path: Path, command: str) -> None:
    assert _metadata(command, tmp_path)["canonical_repo_mutation"] is True


@pytest.mark.parametrize(
    "command",
    [
        "f() \\\n(touch a.py); f",
        "f\\\n() (touch a.py); f",
        "f(\\\n) (touch a.py); f",
        "f\\\n() { touch a.py; }; f",
        "f (\\\n) { touch a.py; }; f",
        "function f\\\n() { touch a.py; }; f",
        "f\\\noo() (touch a.py); foo",
    ],
)
def test_function_header_continuation_reports_write(tmp_path: Path, command: str) -> None:
    metadata = _metadata(command, tmp_path)
    assert metadata["canonical_repo_mutation"] is True
    assert metadata["canonical_file_paths"] == ["a.py"]


def test_function_header_continuation_reuses_heredoc_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = "f\\\n() { touch a.py; }; f\ncat <<'EOF'\n'\\\ntext\nEOF"

    def reject_rescan(subject: str) -> ShellScan:
        assert subject != command, "function header detection rescanned the caller's command"
        return scan_shell_command(subject)

    monkeypatch.setattr(_normalization_compound, "scan_shell_command", reject_rescan)
    metadata = _metadata(command, tmp_path)
    assert metadata["canonical_repo_mutation"] is True
    assert metadata["canonical_file_paths"] == ["a.py"]


@pytest.mark.parametrize(
    "command",
    [
        "touch a.py<<-#\\\ndo",
        "time touch a.py<<-#do time while\\\nx",
    ],
)
def test_heredoc_comment_continuation_reports_write(tmp_path: Path, command: str) -> None:
    metadata = _metadata(command, tmp_path)
    assert metadata["canonical_repo_mutation"] is True
    assert metadata["canonical_file_paths"] == ["a.py"]


def test_quoted_function_header_continuation_stays_literal(tmp_path: Path) -> None:
    metadata = _metadata("echo 'f\\\n() (touch a.py)'", tmp_path)
    assert not metadata.get("canonical_repo_mutation")


@pytest.mark.parametrize(
    "command",
    [
        "cd ..; f() { touch a.py; }; cd 'ROOT'; f",
        "f(){ touch a.py; }; f",
        "echo $(f() { cd /tmp; }; touch a.py)",
        "(cd /tmp; f() (touch a.py); cd 'ROOT'; f)",
        "f() { pushd /tmp; }; touch a.py",
        "f() select d in x; do cd /tmp; break; done; touch a.py",
    ],
)
def test_function_directory_scope_amendment_reports_write(tmp_path: Path, command: str) -> None:
    command = command.replace("ROOT", str(tmp_path))
    assert _metadata(command, tmp_path)["canonical_repo_mutation"] is True


@pytest.mark.timeout(120)
@pytest.mark.parametrize("wrapper", ["echo $({body})", "cat <({body})", "( {body} )"])
def test_nested_wrappers_with_many_functions_report_write(
    tmp_path: Path, wrapper: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = "; ".join([*(f"p{j}() {{ echo {j}; }}" for j in range(1000)), "touch a.py"])
    for i in range(8):
        command = f"f{i}() {{ :; }}; " + wrapper.format(body=command)
    scanned_chars = 0

    def bounded_scan(subject: str) -> ShellScan:
        nonlocal scanned_chars
        scanned_chars += len(subject)
        # Bound cumulative input at this scanner entry point, independently of host load.
        # Measured at e440595: 25.91x input; 32x leaves 23% headroom over that baseline.
        assert scanned_chars <= 32 * len(command), "nested wrapper scan work exceeded linear budget"
        return scan_shell_command(subject)

    monkeypatch.setattr(_normalization_canonical, "scan_shell_command", bounded_scan)
    assert _metadata(command, tmp_path)["canonical_repo_mutation"] is True
    assert scanned_chars >= len(command), "nested wrapper scan work was not observed"


def test_nine_nested_function_definitions_report_write(tmp_path: Path) -> None:
    command = "touch a.py"
    for index in range(9):
        command = f"f{index}() {{ {command}; }}; f{index}"
    assert _metadata(command, tmp_path)["canonical_repo_mutation"] is True


@pytest.mark.parametrize("directory_command", ['"cd"', r"\cd"])
def test_function_called_after_quoted_directory_change_has_conservative_scope(
    tmp_path: Path, directory_command: str
) -> None:
    command = f"cd /tmp; f() (touch a.py); {directory_command} '{tmp_path}'; f"
    assert _metadata(command, tmp_path)["canonical_repo_mutation"] is True


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


@pytest.mark.parametrize("navigation", ["cd", "pushd"])
def test_tilde_directory_target_keeps_home_repository_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, navigation: str
) -> None:
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root))
    data = _metadata(f"cd /tmp; {navigation} ~/sub; touch y.py", root)
    assert data["canonical_file_paths"] == [str(root / "sub" / "y.py")]
    assert data["canonical_repo_mutation"] is True
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize(
    "command",
    [
        "touch ~+/y.py",
        "rm ~-/y.py",
        "touch ~1/y.py",
        "touch ~nosuchuser_zz/y.py",
        "cd ~-; touch y.py",
        "cd /tmp; cd ~-; touch y.py",
        "cd /tmp; cd ~nosuchuser_zz; touch y.py",
    ],
)
def test_unresolvable_tilde_write_fails_closed(tmp_path: Path, command: str) -> None:
    data = _metadata(command, tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Write", {"file_path": "y.py"}),
        ("Edit", {"file_path": "y.py", "old_string": "a", "new_string": "b"}),
        ("Bash", {"command": "touch y.py"}),
        ("Bash", {"command": "echo x > y.py"}),
    ],
)
def test_relative_write_without_cwd_does_not_gain_unknown_scope(
    tool_name: str, tool_input: dict[str, str]
) -> None:
    data: dict[str, Any] = {"tool_name": tool_name, "tool_input": tool_input}
    normalize_tool_fields(data)
    assert data["canonical_repo_mutation"] is True
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize("prefix", ["", "cd /tmp; "])
@pytest.mark.parametrize("navigation", ["cd", "pushd"])
def test_tilde_parent_keeps_repository_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str, navigation: str
) -> None:
    home, root, scratch = (tmp_path / name for name in ("home", "repo", "scratch"))
    for directory in (home, root, scratch):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    data = _metadata(f"{prefix}{navigation} ~/../repo; touch y.py", root, scratch)
    assert data["canonical_file_paths"] == [str(root / "y.py")]
    assert data["canonical_repo_mutation"] is True
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize(
    "command",
    [
        'cd "~"; touch y.py',
        "pushd '~'; touch y.py",
        'touch "~/y.py"',
        "touch '~/y.py'",
        r"touch \~/y.py",
        'echo x > "~/y.py"',
        'cp source.py "~/y.py"',
        'env -C "~" touch y.py',
        'env --chdir="~" touch y.py',
        'env "-C" "~" touch y.py',
    ],
)
def test_quoted_tilde_write_scope_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "outside"))
    data = _metadata(command, root)
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_unquoted_tilde_with_quoted_suffix_keeps_home_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    data = _metadata('touch ~/"y.py"', root)
    assert data["canonical_repo_mutation"] is True
    assert not data.get("canonical_repo_mutation_scope_unknown")


def test_unresolvable_tilde_navigation_does_not_invent_repository_path(tmp_path: Path) -> None:
    data = _metadata("cat ~nosuchuser_zz/../y.py", tmp_path)
    assert not data.get("canonical_file_paths")


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
    (tmp_path / "script.sed").write_text("s/a/b/\n", encoding="utf-8")
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
        ("cd /tmp; eval 'f() { touch a.py; }'; cd -; f", True),
        ("source /tmp/functions.sh; f", True),
        ("select d in x; do cd /tmp; break; done; touch a.py", True),
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


@pytest.mark.parametrize(
    "expression",
    ['"$X"', '"${X}"', '"$(cat {script})"', '"`cat {script}`"', "'eval \"$X\"'"],
)
def test_dynamic_eval_invalidates_followup_scope(tmp_path: Path, expression: str) -> None:
    root = tmp_path / "repo"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    script = scratch / "cd_repo.sh"
    script.write_text(f"cd {root}\n")

    command = f"eval {expression.replace('{script}', str(script))}; touch a.py"
    metadata = _metadata(command, root, cwd=scratch)

    assert metadata["canonical_tool_kind"] == "write"
    assert metadata["canonical_repo_mutation"] is True
    assert metadata["canonical_repo_mutation_scope_unknown"] is True


def test_literal_eval_keeps_followup_external_scope(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()

    metadata = _metadata("eval 'touch a.py'; touch b.py", root, cwd=scratch)

    assert metadata["canonical_tool_kind"] == "write"
    assert not metadata.get("canonical_repo_mutation")
    assert not metadata.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("loader", ["source", "."])
@pytest.mark.parametrize("claimed", [False, True])
async def test_source_refusal_names_execution_route(
    tmp_path: Path, scope_engine: RuleEngine, loader: str, claimed: bool
) -> None:
    script = tmp_path / "setup.sh"
    script.write_text("printf ready\n")
    data = _metadata(f"{loader} {script}", tmp_path)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
    )
    response = await scope_engine.evaluate(
        event,
        session_id=SESSION,
        variables={
            "require_task_before_edit": True,
            "task_claimed": claimed,
            "loaded_skills": ["python", "restraint"],
        },
    )

    assert response.decision == "block"
    assert "uv run" in (response.reason or "")
    assert "literal script path" in (response.reason or "")


@pytest.mark.parametrize(
    "invocation",
    [
        "$CMD {repo}",
        '"${CMD}" {repo}',
        'E=eval; $E "$X"',
        'env FLAG=1 "$CMD" {repo}',
        "$(printf cd) {repo}",
        "`printf cd` {repo}",
    ],
)
def test_dynamic_program_word_invalidates_followup_scope(tmp_path: Path, invocation: str) -> None:
    root = tmp_path / "repo"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()

    command = f"{invocation.replace('{repo}', str(root))}; touch a.py"
    metadata = _metadata(command, root, cwd=scratch)

    assert metadata["canonical_tool_kind"] == "write"
    assert metadata["canonical_script_execution"] is True
    assert metadata["canonical_repo_mutation"] is True
    assert metadata["canonical_repo_mutation_scope_unknown"] is True


def test_top_level_dynamic_program_requires_script_scope(tmp_path: Path) -> None:
    metadata = _metadata('"$CMD"', tmp_path, cwd=tmp_path)

    assert metadata["canonical_script_execution"] is True
    assert metadata["canonical_tool_kind"] == "execute"
    assert metadata["canonical_tool_confidence"] == "low"


@pytest.mark.parametrize("condition", ['[[ -n "$X" ]]', "(( $X > 0 ))"])
def test_compound_condition_preserves_followup_scope(tmp_path: Path, condition: str) -> None:
    root = tmp_path / "repo"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()

    metadata = _metadata(f"{condition}; touch a.py", root, cwd=scratch)

    assert metadata["canonical_tool_kind"] == "write"
    assert metadata["canonical_write_file_paths"] == ["a.py"]
    assert not metadata.get("canonical_script_execution")
    assert not metadata.get("canonical_repo_mutation")
    assert not metadata.get("canonical_repo_mutation_scope_unknown")
