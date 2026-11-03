"""Integration coverage for bundled Impeccable detector rules."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gobby.skills.materialization import (
    SkillScriptMaterializer,
)
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def impeccable_db(temp_db: HubDatabase) -> HubDatabase:
    manager = RuleDefinitionManager(temp_db)
    for index in range(2):
        manager.create(
            f"synthetic-prewarm-{index}",
            {
                "event": "before_tool",
                "effects": [
                    {
                        "type": "run_command",
                        "command": ["unused"],
                        "skill": "synthetic-skill",
                        "script": "scripts/check.py",
                    }
                ],
            },
            tags=["default"],
        )
    return temp_db


@pytest.mark.asyncio
async def test_prewarm_materializes_each_enabled_rule_skill_once(
    impeccable_db: HubDatabase,
) -> None:
    materializer = AsyncMock(spec=SkillScriptMaterializer)
    resolved: list[tuple[str, str | None]] = []

    async def resolve(name: str, *, project_id: str | None) -> None:
        resolved.append((name, project_id))

    materializer.resolve.side_effect = resolve
    engine = RuleEngine(impeccable_db, skill_script_materializer=materializer)

    await engine.prewarm_skill_scripts(project_id=None)

    assert resolved == [("synthetic-skill", None)]


@pytest.mark.asyncio
async def test_prewarm_failure_is_non_blocking(
    impeccable_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_id = "00000000-0000-0000-0000-000000000123"
    materializer = AsyncMock(spec=SkillScriptMaterializer)
    resolved: list[tuple[str, str | None]] = []

    async def fail(name: str, *, project_id: str | None) -> None:
        resolved.append((name, project_id))
        raise RuntimeError("sensitive cache path")

    materializer.resolve.side_effect = fail
    engine = RuleEngine(impeccable_db, skill_script_materializer=materializer)

    with caplog.at_level("WARNING"):
        await engine.prewarm_skill_scripts(project_id=project_id)

    assert resolved == [("synthetic-skill", project_id)]
    assert f"skill synthetic-skill in project {project_id}" in caplog.text
    assert "RuntimeError: sensitive cache path" in caplog.text


async def _run_node_eval(script: str) -> str:
    command = ["node", "--input-type=module", "--eval", script]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5.0)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    assert process.returncode is not None
    if process.returncode != 0:
        raise subprocess.CalledProcessError(
            process.returncode,
            command,
            output=stdout.decode(),
            stderr=stderr.decode(),
        )
    return stdout.decode()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
async def test_hook_project_root_resolves_nested_worktree_cwd(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    nested = worktree / "packages" / "web" / "src"
    nested.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../repo/.git/worktrees/test\n")
    helper = (
        Path(__file__).parents[2]
        / "src/gobby/install/shared/skills/impeccable/scripts/hook-project-root.mjs"
    )
    event_json = json.dumps({"cwd": str(nested)})
    script = (
        f"import {{ resolveHookProjectCwd }} from {json.dumps(helper.as_uri())}; "
        f"process.stdout.write(resolveHookProjectCwd({json.dumps(event_json)}, "
        f"{json.dumps(str(tmp_path))}));"
    )

    assert await _run_node_eval(script) == str(worktree)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
@pytest.mark.parametrize("event", [True, {"cwd": True}, {"workspace_roots": [42]}])
async def test_hook_project_root_rejects_non_string_path_values(
    tmp_path: Path, event: object
) -> None:
    helper = (
        Path(__file__).parents[2]
        / "src/gobby/install/shared/skills/impeccable/scripts/hook-project-root.mjs"
    )
    event_json = json.dumps(event)
    script = (
        f"import {{ resolveHookProjectCwd }} from {json.dumps(helper.as_uri())}; "
        f"process.stdout.write(resolveHookProjectCwd({json.dumps(event_json)}, "
        f"{json.dumps(str(tmp_path))}));"
    )

    assert await _run_node_eval(script) == str(tmp_path)
