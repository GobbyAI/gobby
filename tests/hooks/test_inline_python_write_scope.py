"""Inline Python write scope as block-unresolved-scope-shell-write sees it (#23759)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.hooks._normalization_tools import _set_canonical_tool_metadata

pytestmark = pytest.mark.unit


# An untrusted generator with an import, a local helper, and two
# literal absolute write targets inside the session's own worktree.
_SEAT_GENERATOR = """\
from pathlib import Path
import yaml
root = Path('{agents}')
baseline = yaml.safe_load((root / 'default.yaml').read_text())['prompts']['persona']
platform = baseline[baseline.index('## Fixture Context'):].strip()
def make(name: str, description: str) -> str:
    role = (Path('{roles}') / (name + '.md')).read_text().strip()
    text = f'name: {{name}}\\ndescription: |\\n  {{description}}\\n'
    if name == 'fixture-second':
        for tool in yaml.safe_load((root / 'fixture-policy.yaml').read_text())['blocked_tools']:
            text += f'  - "{{tool}}"\\n'
    prompt = '## Role\\n' + role + '\\n\\n' + platform
    text += ''.join('    ' + line + '\\n' if line else '\\n' for line in prompt.splitlines())
    return text + '  persona: *seat\\n'
Path('{first}').write_text(make('fixture-first', 'First fixture.'))
Path('{second}').write_text(make('fixture-second', 'Second fixture.'))
"""

# 15399's read-only diagnostics: they import a private helper from a test module.
_READ_ONLY_DIAGNOSTICS = [
    "uv run python -c 'from pathlib import Path; from tests.fixtures.e2e_run_cleanup import "
    "_resources; import os; print([(r.process.pid,str(r.socket_dir)) for r in "
    '_resources(os.environ.get("GOBBY_E2E_RUN_ID", "no-run"))]); '
    'paths=sorted(Path("/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/pytest-of-josh")'
    '.glob("pytest-current/**/pytest.log")); print([(str(p),p.read_text()[-4000:]) for p in paths])\'',
    "uv run python -c 'import time; from tests.fixtures.e2e_run_cleanup import _resources; "
    'started = time.monotonic(); resources = _resources("no-such-run"); '
    'print("scan_seconds", time.monotonic() - started, "resources", len(resources))\'',
]


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "lane-3-runbooks-4"
    (path / "src/gobby/install/shared/workflows/agents").mkdir(parents=True)
    return path


def _metadata(command: str, worktree: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "workdir": str(worktree)},
        "project_path": str(worktree),
    }
    _set_canonical_tool_metadata(data)
    return data


def _seat_generator_command(worktree: Path, first: Path, second: Path) -> str:
    script = _SEAT_GENERATOR.format(
        agents=worktree / "src/gobby/install/shared/workflows/agents",
        roles=worktree.parent / "roles",
        first=first,
        second=second,
    )
    return f"uv run python - <<'PY'\n{script}PY"


def test_seat_generator_heredoc_attributes_its_literal_worktree_targets(worktree: Path) -> None:
    agents = worktree / "src/gobby/install/shared/workflows/agents"
    first, second = agents / "fixture-first.yaml", agents / "fixture-second.yaml"

    data = _metadata(_seat_generator_command(worktree, first, second), worktree)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert not data.get("canonical_repo_mutation_scope_unknown")
    assert data["canonical_write_file_paths"] == [str(first), str(second)]


def test_untrusted_inline_program_with_literal_worktree_target_is_attributed(
    worktree: Path,
) -> None:
    target = worktree / "src/gobby/install/shared/workflows/agents/fixture-first.yaml"
    command = (
        'uv run python -c "import yaml; from pathlib import Path; '
        f"Path('{target}').write_text(yaml.safe_dump({{'name': 'fixture-first'}}))\""
    )

    data = _metadata(command, worktree)

    assert data["canonical_repo_mutation"] is True
    assert not data.get("canonical_repo_mutation_scope_unknown")
    assert data["canonical_write_file_paths"] == [str(target)]


@pytest.mark.parametrize("command", _READ_ONLY_DIAGNOSTICS, ids=["e2e-resources", "scan-timing"])
def test_read_only_diagnostic_importing_a_private_helper_is_not_a_write(
    worktree: Path, command: str
) -> None:
    data = _metadata(command, worktree)

    assert data["canonical_tool_kind"] == "execute"
    assert not data.get("canonical_repo_mutation")
    assert not data.get("canonical_repo_mutation_scope_unknown")


def test_untrusted_program_with_a_target_outside_the_project_still_blocks(
    worktree: Path,
) -> None:
    inside = worktree / "src/gobby/install/shared/workflows/agents/fixture-first.yaml"
    outside = worktree.parent / "scratch" / "fixture-second.yaml"

    data = _metadata(_seat_generator_command(worktree, inside, outside), worktree)

    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_untrusted_program_with_an_unresolvable_target_still_blocks(worktree: Path) -> None:
    command = (
        "uv run python - <<'PY'\nimport yaml\nfrom pathlib import Path\n"
        "Path(input()).write_text(yaml.safe_dump({'name': 'merge-manager'}))\nPY"
    )

    data = _metadata(command, worktree)

    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True
