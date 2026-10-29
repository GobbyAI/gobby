"""Submit permits audits and reads while keeping the submitted candidate frozen."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from tests.workflows.test_seat_step_loops import SeatRun
from tests.workflows.test_submit_review_recovery import _admits, _submitted

pytestmark = pytest.mark.unit
_RULES = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/install/shared/workflows/rules/roles/seat-write-scope.yaml"
)


async def _submit_seat(db: HubDatabase) -> SeatRun:
    run, _ = await _submitted(db)
    bundle = yaml.safe_load(_RULES.read_text())
    for name, rule in bundle["rules"].items():
        if name.startswith("developer-submit-"):
            body = {
                key: value
                for key, value in rule.items()
                if key not in {"description", "enabled", "priority"}
            }
            run.engine.rule_manager.create(
                name=name,
                definition_json=RuleDefinitionBody.model_validate(body).model_dump_json(),
                enabled=True,
                priority=rule["priority"],
                tags=bundle["tags"],
            )
    return run


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        'git -C "/tmp/repo name" --no-pager diff --stat',
        "git branch --show-current",
        "git tag --list",
        "uv run --no-sync gobby test-quality audit tests/test_example.py --fail-on-new",
        "uv run --no-sync gobby test-types audit tests/test_example.py --fail-on-new",
        "uv run --no-sync ruff format --check src/example.py",
        "uv run --no-sync ruff check src/example.py",
        "GOBBY_TEST_PROTECT=1 uv run --no-sync pytest tests/test_example.py -q",
    ],
)
async def test_submit_allows_read_and_audit_commands(temp_db: HubDatabase, command: str) -> None:
    run = await _submit_seat(temp_db)
    assert await _admits(run, "Bash", {"command": command})
    assert run.step == "submit"


@pytest.mark.parametrize(
    "command",
    [
        "printf changed > src/example.py",
        "git commit -am changed",
        'git -C "/tmp/repo name" --no-optional-locks commit -am changed',
        "git --git-dir=/tmp/repo/.git --work-tree=/tmp/repo reset --hard HEAD",
        "git -c alias.save='commit -am changed' save",
        "git saved-alias",
        "git branch replacement HEAD",
        "git tag candidate HEAD",
        "git update-ref refs/heads/main HEAD",
        "git hash-object -w src/example.py",
        "git add src/example.py",
        "git switch another-branch",
        "git diff --output=changes.patch",
        "uv run --no-sync ruff format src/example.py",
        "uv run --no-sync ruff check --fix src/example.py",
        "uv run --no-sync black src/example.py",
        "npx prettier --write src/example.ts",
        "cargo fmt",
    ],
)
async def test_submit_refuses_candidate_mutation(temp_db: HubDatabase, command: str) -> None:
    run = await _submit_seat(temp_db)
    assert not await _admits(run, "Bash", {"command": command})
    assert run.step == "submit"


async def test_submit_reads_files_but_refuses_native_edits(temp_db: HubDatabase) -> None:
    run = await _submit_seat(temp_db)
    assert await _admits(run, "Read", {"file_path": "src/example.py"})
    assert not await _admits(run, "Edit", {"file_path": "src/example.py"})
    assert not await _admits(run, "Write", {"file_path": "src/example.py", "content": "changed"})
