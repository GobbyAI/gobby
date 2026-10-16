"""Read-only shell and inline-Python commands carry no unresolved write scope.

Parse-only shells, ``sed -n`` with bound or arithmetic addresses, and inline
Python that only reads stay unblocked; their write-capable variants stay writes.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_rule_file

pytestmark = pytest.mark.unit

SESSION = "22222222-2222-4222-8222-222222222222"
SCRIPT = "pre-push-test.sh"

# 16495's repro: a crate tarball inspected in memory, never extracted.
TARFILE_READ = (
    'uv run python -c "import io, tarfile, urllib.request\n'
    "data = urllib.request.urlopen('https://example.com/x.crate').read()\n"
    "t = tarfile.open(fileobj=io.BytesIO(data), mode='r:gz')\n"
    "print(t.extractfile('x/Cargo.toml').read()[:200])\n"
    '"'
)
_PSUTIL_INVENTORY = (
    "uv run --no-sync python -c 'import psutil{imports}; from pathlib import Path; "
    "root = str(Path.cwd()); matches = []; \n"
    'for p in psutil.process_iter(["pid", "cmdline"]):\n'
    " try:\n"
    '  cmd = p.info["cmdline"] or []\n'
    '  if any("pytest" in arg for arg in cmd) and p.cwd() == root and '
    "p.pid != {os}.getpid(): matches.append((p.pid, cmd))\n"
    " except (psutil.NoSuchProcess, psutil.AccessDenied): pass\n"
    'print({{"owned_pid5439_exists": psutil.pid_exists(5439), "own_wt_pytest": matches}})\''
)
# 15994's process inventory, importing os up front.
PSUTIL_INVENTORY = _PSUTIL_INVENTORY.format(imports=", os", os="os")
# 15994's exact inventory: `__import__` is a reflection escape, fail-closed by PD's ruling.
PSUTIL_INVENTORY_REFLECTED = _PSUTIL_INVENTORY.format(imports="", os='__import__("os")')


def _metadata(command: str, root: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(root)},
        "project_path": str(root),
    }
    normalize_tool_fields(data)
    return data


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / SCRIPT).write_text("touch a.py\necho hi > out.txt\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("## T\nline\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "command",
    [
        f"bash -n {SCRIPT}",
        f"/bin/bash -n {{root}}/{SCRIPT}",
        f"sh -n {SCRIPT}",
        f"zsh -n {SCRIPT}",
        f"bash -nx {SCRIPT}",
        f"bash -o noexec {SCRIPT}",
        f"zsh +o exec {SCRIPT}",
        f"bash -n -- {SCRIPT}",
    ],
)
def test_parse_only_shell_reads_its_script(repo: Path, command: str) -> None:
    data = _metadata(command.format(root=repo), repo)

    assert data["canonical_tool_kind"] == "read"
    assert data.get("canonical_repo_mutation") is not True
    assert data["canonical_file_paths"] == [f"{repo}/{SCRIPT}" if "{root}" in command else SCRIPT]


@pytest.mark.parametrize(
    "command",
    ["bash -n -c 'touch a.py'", "bash -nc 'touch a.py'", "sh -n -c 'touch a.py'"],
)
def test_parse_only_shell_program_executes_nothing(repo: Path, command: str) -> None:
    data = _metadata(command, repo)

    assert data["canonical_tool_kind"] == "execute"
    assert data.get("canonical_repo_mutation") is not True


@pytest.mark.parametrize(
    "command",
    [
        f"bash {SCRIPT}",
        f"bash -x {SCRIPT}",
        f"bash -n +n {SCRIPT}",
        f"bash -o noexec +o noexec {SCRIPT}",
        f"bash -ni {SCRIPT}",
    ],
)
def test_executing_shell_keeps_script_writes(repo: Path, command: str) -> None:
    data = _metadata(command, repo)

    assert data["canonical_tool_kind"] == "write"
    assert sorted(data["canonical_write_file_paths"]) == ["a.py", "out.txt"]


@pytest.mark.parametrize(
    "command",
    [
        'a=10; b=20; sed -n "${a},${b}p" src/x.py',
        'N=5; sed -n "$N"p src/x.py',
        'a=1 && sed -n "${a},\\$p" src/x.py',
        'f=notes.md; n=$(grep -n "^## T" $f | cut -d: -f1); sed -n "$((n+1)),$((n+8))p" $f',
        'sed -n "$((n+1)),$((n + 8))p" notes.md',
        'a=1; sed -n -e "${a}p" src/x.py | head -3',
    ],
)
def test_sed_with_bound_or_arithmetic_addresses_reads(repo: Path, command: str) -> None:
    data = _metadata(command, repo)

    assert data["canonical_tool_kind"] == "read"
    assert data.get("canonical_repo_mutation") is not True
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize(
    "command",
    [
        'sed -n "${a},${b}p" src/x.py',
        'a=$(cat cfg); sed -n "${a}p" src/x.py',
        "a='1w out.txt'; sed -n \"${a}\" src/x.py",
        "a='1e touch x'; sed -n \"${a}\" src/x.py",
        "a=10; echo; a='1w out.txt'; sed -n \"${a}p\" src/x.py",
        "a=10; sed -n ${a}p src/x.py",
        'a=1; sed -n "${a}w out.txt" src/x.py',
        'sed -n "$((n))w out.txt" src/x.py',
        'sed -n "$(echo 1w out.txt)" src/x.py',
        'sed -n "${a:-1w out.txt}" src/x.py',
    ],
)
def test_sed_with_unproven_program_stays_unknown_write(repo: Path, command: str) -> None:
    data = _metadata(command, repo)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "command,writes",
    [
        ("sed -i 's/a/b/' src/x.py", ["src/x.py"]),
        ("sed -n '1w out.txt' src/x.py", ["out.txt"]),
        ("sed -n '1W out.txt' src/x.py", ["out.txt"]),
    ],
)
def test_sed_literal_writes_stay_writes(repo: Path, command: str, writes: list[str]) -> None:
    data = _metadata(command, repo)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_write_file_paths"] == writes


@pytest.mark.parametrize("command", [TARFILE_READ, PSUTIL_INVENTORY])
def test_inline_python_reads_are_not_writes(repo: Path, command: str) -> None:
    data = _metadata(command, repo)

    assert data["canonical_tool_kind"] == "execute"
    assert data.get("canonical_repo_mutation") is not True
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize(
    "script,writes",
    [
        ("import tarfile; tarfile.open('x.tgz').extractall('out')", ["out"]),
        ("import tarfile; t = tarfile.open('x.tgz'); t.extract('m', 'out')", ["out"]),
        ("import tarfile; tarfile.open('x.tgz').extractall(path='out')", ["out"]),
        ("import zipfile; zipfile.ZipFile('x.zip').extractall('out')", ["out"]),
        ("import zipfile; zipfile.ZipFile('x.zip').extract('m', path='out')", ["out"]),
        ("import shutil; shutil.unpack_archive('x.zip', 'out')", ["out"]),
        ("import tarfile; tarfile.open('out.tar', 'w')", ["out.tar"]),
        ("open('out.txt', 'w').write('x')", ["out.txt"]),
    ],
)
def test_inline_python_writes_name_their_targets(
    repo: Path, script: str, writes: list[str]
) -> None:
    data = _metadata(f'uv run python -c "{script}"', repo)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_write_file_paths"] == writes


@pytest.mark.parametrize(
    "script",
    [
        "import tarfile; tarfile.open('x.tgz').extractall()",
        "import tarfile, sys; tarfile.open('x.tgz').extractall(sys.argv[1])",
        "import zipfile; zipfile.ZipFile('x.zip').extract('m')",
        "import shutil; shutil.unpack_archive('x.zip')",
        "__import__('os').remove('out.txt')",
        "__import__('os').getpid()",
        "f = __import__; f('os')",
    ],
)
def test_inline_python_unproven_writes_stay_unknown(repo: Path, script: str) -> None:
    data = _metadata(f'uv run python -c "{script}"', repo)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.fixture
def scope_engine(hub_db: HubDatabase) -> RuleEngine:
    path = get_bundled_rules_path() / "task-enforcement/block-unresolved-scope-shell-write.yaml"
    assert sync_rule_file(hub_db, path, tag="gobby")["success"] is True
    hub_db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return RuleEngine(hub_db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,blocked",
    [
        (f"bash -n {SCRIPT}", False),
        (f"/bin/bash -n {{root}}/{SCRIPT}", False),
        (f"sh -n {SCRIPT}", False),
        (f"zsh -n {SCRIPT}", False),
        ('a=10; b=20; sed -n "${a},${b}p" src/x.py', False),
        ('N=5; sed -n "$N"p src/x.py', False),
        (
            'f=notes.md; n=$(grep -n "^## T" $f | cut -d: -f1); sed -n "$((n+1)),$((n+8))p" $f',
            False,
        ),
        (TARFILE_READ, False),
        (PSUTIL_INVENTORY, False),
        (PSUTIL_INVENTORY_REFLECTED, True),
        ('sed -n "${a},${b}p" src/x.py', True),
        ("a='1w out.txt'; sed -n \"${a}\" src/x.py", True),
        ("uv run python -c \"import tarfile; tarfile.open('x.tgz').extractall()\"", True),
    ],
)
async def test_unresolved_scope_rule_blocks_only_unproven_writes(
    repo: Path, scope_engine: RuleEngine, command: str, blocked: bool
) -> None:
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=_metadata(command.format(root=repo) if "{root}" in command else command, repo),
    )

    result = await scope_engine.evaluate(event, session_id=SESSION, variables={})

    assert result.decision == ("block" if blocked else "allow"), result.reason
