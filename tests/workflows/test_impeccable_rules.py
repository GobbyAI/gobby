"""Integration coverage for bundled Impeccable detector rules."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.skills.materialization import (
    NodeRuntimeResult,
    PreparationResult,
    SkillMaterializationResult,
    SkillScriptMaterializer,
)
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.run_command import RunCommandResult
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
PROVIDERS = [
    SessionSource.CLAUDE,
    SessionSource.CODEX,
    SessionSource.QWEN,
    SessionSource.DROID,
    SessionSource.GROK,
    SessionSource.AGY,
]


@pytest.fixture
def impeccable_db(temp_db: HubDatabase) -> HubDatabase:
    rules_path = get_bundled_rules_path() / "impeccable"
    result = sync_bundled_rules(temp_db, rules_path)
    assert result["success"] is True
    return temp_db


def _event(source: SessionSource, event_type: HookEventType) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=f"{source.value}-session",
        source=source,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "Write",
            "tool_input": {"file_path": "src/Card.tsx"},
        },
        cwd=str(Path.cwd()),
        metadata={},
    )


def test_impeccable_templates_sync_enabled(impeccable_db: HubDatabase) -> None:
    manager = RuleDefinitionManager(impeccable_db)

    edit = manager.get_by_name("impeccable-edit-pass")
    deep = manager.get_by_name("impeccable-deep-pass")

    assert edit is not None and edit.enabled is True
    assert deep is not None and deep.enabled is True
    assert "enforcement" in (edit.tags or [])
    assert "enforcement" in (deep.tags or [])

    edit_definition = RuleDefinitionBody.model_validate(edit.definition_json)
    deep_definition = RuleDefinitionBody.model_validate(deep.definition_json)
    assert [effect.type for effect in edit_definition.resolved_effects] == [
        "set_variable",
        "run_command",
    ]
    assert [effect.type for effect in deep_definition.resolved_effects] == [
        "set_variable",
        "run_command",
    ]
    edit_command = edit_definition.resolved_effects[1]
    deep_command = deep_definition.resolved_effects[1]
    assert edit_command.command == ["node"]
    assert edit_command.skill == "impeccable"
    assert edit_command.script == "hook.mjs"
    assert deep_command.command == ["node"]
    assert deep_command.skill == "impeccable"
    assert deep_command.script == "hook.mjs"
    assert deep_definition.when == "variables.get('impeccable_ui_edited_this_turn', False)"


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

    assert resolved == [("impeccable", None)]


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

    assert resolved == [("impeccable", project_id)]
    assert f"skill impeccable in project {project_id}" in caplog.text
    assert "RuntimeError: sensitive cache path" in caplog.text


@pytest.mark.parametrize("source", PROVIDERS)
async def test_edit_detector_evaluates_for_supported_sources(
    impeccable_db: HubDatabase,
    source: SessionSource,
    tmp_path: Path,
) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "hook.mjs").write_text("export {};\n")
    materializer = AsyncMock(spec=SkillScriptMaterializer)
    materializer.resolve.return_value = SkillMaterializationResult(
        scripts_dir=scripts_dir,
        files_written=1,
        environment={},
        parser_deps=PreparationResult(ready=True, warning=None),
        browser=PreparationResult(ready=True, warning=None),
        node=NodeRuntimeResult(version=None, satisfies_floor=None),
    )
    engine = RuleEngine(impeccable_db, skill_script_materializer=materializer)
    execute = AsyncMock(
        return_value=RunCommandResult(
            status="success",
            context=f"finding from {source.value}",
            duration_ms=1.0,
            exit_code=0,
            stdout_bytes=10,
            stderr_bytes=0,
            timeout_seconds=5.0,
            overflow_stream=None,
            background=False,
            phase="execution",
            skill="impeccable",
            script="hook.mjs",
        )
    )
    with patch.object(engine, "_execute_run_command", execute):
        variables: dict[str, object] = {}
        response = await engine.evaluate(
            _event(source, HookEventType.AFTER_TOOL),
            session_id=SESSION_ID,
            variables=variables,
        )

    assert response.decision == "allow"
    assert response.context == f"finding from {source.value}"
    assert variables["impeccable_ui_edited_this_turn"] is True
    execute.assert_awaited_once()


@pytest.mark.parametrize("source", PROVIDERS)
async def test_deep_detector_schedules_for_supported_sources(
    impeccable_db: HubDatabase,
    source: SessionSource,
) -> None:
    engine = RuleEngine(impeccable_db)
    variables: dict[str, object] = {}
    with patch("gobby.workflows.engine.effects.create_background_task") as create_task:
        skipped = await engine.evaluate(
            _event(source, HookEventType.STOP),
            session_id=SESSION_ID,
            variables=variables,
        )
        create_task.assert_not_called()

        variables["impeccable_ui_edited_this_turn"] = True
        response = await engine.evaluate(
            _event(source, HookEventType.STOP),
            session_id=SESSION_ID,
            variables=variables,
        )

    assert skipped.decision == "allow"
    assert response.decision == "allow"
    assert variables["impeccable_ui_edited_this_turn"] is False
    create_task.assert_called_once()
    create_task.call_args.args[0].close()


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
