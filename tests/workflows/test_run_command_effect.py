"""Tests for the run_command rule effect."""

import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.skills.materialization import (
    NodeRuntimeResult,
    PreparationResult,
    SkillMaterializationResult,
)
from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.workflows.definitions import RuleEffect
from gobby.workflows.engine.effects import EffectsMixin
from gobby.workflows.engine.run_command import (
    STDERR_LIMIT_BYTES,
    STDOUT_LIMIT_BYTES,
    RunCommandResult,
    _parse_command_output,
    build_run_command_payload,
    execute_run_command,
    resolve_materialized_skill_script,
)

pytestmark = pytest.mark.unit

_ROW = cast(RuleDefinitionRow, SimpleNamespace(id="rule-id", name="test-rule"))

# Reads the hook event JSON from stdin and echoes its tool name back through
# the Claude hook response shape — proves both stdin fidelity and extraction.
_ECHO_SCRIPT = (
    "import json, sys; "
    "event = json.load(sys.stdin); "
    "print(json.dumps({'hookSpecificOutput': "
    "{'additionalContext': 'saw ' + event['tool_name']}}))"
)


def _event(**data: Any) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="ext-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Edit", **data},
        cwd=None,
        metadata={"_platform_session_id": "platform-session-1"},
    )


def _effect(**overrides: Any) -> RuleEffect:
    defaults: dict[str, Any] = {
        "type": "run_command",
        "command": [sys.executable, "-c", _ECHO_SCRIPT],
        "inject_result": True,
    }
    defaults.update(overrides)
    return RuleEffect(**defaults)


async def _apply(effect: RuleEffect, event: HookEvent) -> list[str]:
    context_parts: list[str] = []
    await EffectsMixin()._apply_effect(
        effect, _ROW, {}, {"event": event}, {}, context_parts, [], {}
    )
    return context_parts


def _materialized_result(scripts_dir: Path, browser_cache: Path) -> SkillMaterializationResult:
    return SkillMaterializationResult(
        scripts_dir=scripts_dir,
        files_written=1,
        environment={"PUPPETEER_CACHE_DIR": str(browser_cache)},
        parser_deps=PreparationResult(ready=True, warning=None),
        browser=PreparationResult(ready=True, warning=None),
        node=NodeRuntimeResult(version=None, satisfies_floor=None),
    )


@asynccontextmanager
async def _execution_guard(_scripts_dir: Path) -> AsyncIterator[None]:
    yield


def _materializer(resolve: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(resolve=resolve, execution_guard=_execution_guard)


@pytest.mark.asyncio
async def test_skill_command_uses_materialized_script_from_event_cwd(tmp_path: Path) -> None:
    event_cwd = tmp_path / "worktree" / "frontend"
    event_cwd.mkdir(parents=True)
    scripts_dir = tmp_path / "cache" / "scripts"
    scripts_dir.mkdir(parents=True)
    script = scripts_dir / "hook.py"
    script.write_text(
        "import json, os, sys\n"
        "event = json.load(sys.stdin)\n"
        "context = '|'.join((os.getcwd(), event['tool_name'], "
        "os.environ['PUPPETEER_CACHE_DIR']))\n"
        "print(json.dumps({'hookSpecificOutput': {'additionalContext': context}}))\n"
    )
    materialized = _materialized_result(scripts_dir, tmp_path / "browser-cache")
    mixin = EffectsMixin()
    resolve = AsyncMock(return_value=materialized)
    mixin.skill_script_materializer = cast(Any, _materializer(resolve))
    event = _event()
    event.cwd = str(event_cwd)
    event.project_id = "project-id"
    context_parts: list[str] = []

    await mixin._apply_effect(
        _effect(
            command=[sys.executable],
            skill="impeccable",
            script="hook.py",
        ),
        _ROW,
        {},
        {"event": event},
        {},
        context_parts,
        [],
        {},
    )

    assert context_parts == [f"{event_cwd}|Edit|{tmp_path / 'browser-cache'}"]
    resolve.assert_awaited_once_with("impeccable", project_id="project-id")
    assert not (event_cwd / ".agents").exists()


@pytest.mark.asyncio
async def test_skill_command_revalidates_script_while_spawn_guard_is_held(
    tmp_path: Path,
) -> None:
    scripts_dir = tmp_path / "cache" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "hook.py").write_text("print('{}')\n")
    materialized = _materialized_result(scripts_dir, tmp_path / "browser-cache")
    guard_held = False
    validation_states: list[bool] = []

    @asynccontextmanager
    async def execution_guard(_scripts_dir: Path) -> AsyncIterator[None]:
        nonlocal guard_held
        guard_held = True
        try:
            yield
        finally:
            guard_held = False

    def tracked_resolve(root: Path, script: str) -> Path:
        validation_states.append(guard_held)
        return resolve_materialized_skill_script(root, script)

    mixin = EffectsMixin()
    resolve = AsyncMock(return_value=materialized)
    mixin.skill_script_materializer = cast(
        Any,
        SimpleNamespace(resolve=resolve, execution_guard=execution_guard),
    )

    with patch(
        "gobby.workflows.engine.effects.resolve_materialized_skill_script",
        side_effect=tracked_resolve,
    ):
        await mixin._apply_effect(
            _effect(command=[sys.executable], skill="impeccable", script="hook.py"),
            _ROW,
            {},
            {"event": _event()},
            {},
            [],
            [],
            {},
        )

    assert validation_states == [False, True]


@pytest.mark.asyncio
async def test_background_skill_command_uses_event_cwd_without_agents_tree(tmp_path: Path) -> None:
    event_cwd = tmp_path / "worktree" / "frontend"
    event_cwd.mkdir(parents=True)
    scripts_dir = tmp_path / "cache" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "hook.py").write_text(
        "import json, os, pathlib, sys\n"
        "event = json.load(sys.stdin)\n"
        "pathlib.Path('detector-result.json').write_text(json.dumps({"
        "'cwd': os.getcwd(), 'tool': event['tool_name'], "
        "'cache': os.environ['PUPPETEER_CACHE_DIR']}))\n"
        "print('{}')\n"
    )
    mixin = EffectsMixin()
    resolve = AsyncMock(return_value=_materialized_result(scripts_dir, tmp_path / "browser-cache"))
    mixin.skill_script_materializer = cast(Any, _materializer(resolve))
    event = _event()
    event.cwd = str(event_cwd)
    event.project_id = "project-id"

    await mixin._apply_effect(
        _effect(
            command=[sys.executable],
            skill="impeccable",
            script="hook.py",
            background=True,
            timeout_seconds=2.0,
        ),
        _ROW,
        {},
        {"event": event},
        {},
        [],
        [],
        {},
    )
    task = next(iter(mixin._background_run_command_registry().values()))
    await task

    assert json.loads((event_cwd / "detector-result.json").read_text()) == {
        "cwd": str(event_cwd),
        "tool": "Edit",
        "cache": str(tmp_path / "browser-cache"),
    }
    resolve.assert_awaited_once_with("impeccable", project_id="project-id")
    assert not (event_cwd / ".agents").exists()


@pytest.mark.asyncio
class TestRunCommandInline:
    async def test_success_injects_context_from_stdin_payload(self) -> None:
        context_parts = await _apply(_effect(), _event())
        assert context_parts == ["saw Edit"]

    async def test_success_without_inject_result_appends_nothing(self) -> None:
        context_parts = await _apply(_effect(inject_result=False), _event())
        assert context_parts == []

    async def test_nonzero_exit_fails_open(self) -> None:
        effect = _effect(command=[sys.executable, "-c", "import sys; sys.exit(3)"])
        assert await _apply(effect, _event()) == []

    async def test_missing_executable_fails_open(self) -> None:
        effect = _effect(command=["/nonexistent/gobby-test-binary"])
        assert await _apply(effect, _event()) == []

    async def test_timeout_kills_process_and_fails_open(self) -> None:
        effect = _effect(
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=0.2,
        )
        assert await _apply(effect, _event()) == []

    async def test_non_json_stdout_fails_open(self) -> None:
        effect = _effect(command=[sys.executable, "-c", "print('plain text')"])
        assert await _apply(effect, _event()) == []

    async def test_missing_event_is_noop(self) -> None:
        context_parts: list[str] = []
        await EffectsMixin()._apply_effect(_effect(), _ROW, {}, {}, {}, context_parts, [], {})
        assert context_parts == []


@pytest.mark.asyncio
class TestRunCommandBackground:
    async def test_background_schedules_task_and_injects_nothing_inline(self) -> None:
        effect = _effect(background=True)
        context_parts: list[str] = []
        with patch("gobby.workflows.engine.effects.create_background_task") as mock_create:
            await EffectsMixin()._apply_effect(
                effect, _ROW, {}, {"event": _event()}, {}, context_parts, [], {}
            )
        assert mock_create.call_count == 1
        assert context_parts == []
        coro = mock_create.call_args.args[0]
        coro.close()

    async def test_deliver_writes_command_result_message(self) -> None:
        mixin = EffectsMixin()
        mixin.db = MagicMock()
        manager = MagicMock()
        messages: list[dict[str, str]] = []

        def record_message(**kwargs: str) -> None:
            messages.append(kwargs)

        manager.create_message.side_effect = record_message
        with patch(
            "gobby.storage.inter_session_messages.InterSessionMessageManager",
            return_value=manager,
        ):
            await mixin._run_command_then_deliver(
                [sys.executable, "-c", _ECHO_SCRIPT],
                os.getcwd(),
                json.dumps({"tool_name": "Write"}).encode(),
                5.0,
                project_id=None,
                skill=None,
                script=None,
                rule_name="test-rule",
                rule_id="rule-id",
                platform_session_id="platform-session-1",
            )
        assert messages == [
            {
                "from_session": "platform-session-1",
                "to_session": "platform-session-1",
                "content": "saw Write",
                "message_type": "command_result",
            }
        ]

    async def test_deliver_skips_without_platform_session(self) -> None:
        mixin = EffectsMixin()
        mixin.db = MagicMock()
        constructions: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def record_construction(*args: object, **kwargs: object) -> MagicMock:
            constructions.append((args, kwargs))
            return MagicMock()

        with patch(
            "gobby.storage.inter_session_messages.InterSessionMessageManager",
            side_effect=record_construction,
        ):
            await mixin._run_command_then_deliver(
                [sys.executable, "-c", _ECHO_SCRIPT],
                os.getcwd(),
                json.dumps({"tool_name": "Write"}).encode(),
                5.0,
                project_id=None,
                skill=None,
                script=None,
                rule_name="test-rule",
                rule_id="rule-id",
                platform_session_id=None,
            )
        assert constructions == []

    async def test_single_flight_registry_cleans_up_after_completion(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mixin = EffectsMixin()
        started = asyncio.Event()
        release = asyncio.Event()

        async def gated_run(*args: object, **kwargs: object) -> None:
            started.set()
            await release.wait()

        run = AsyncMock(side_effect=gated_run)
        event = _event()
        with patch.object(mixin, "_run_command_then_deliver", run), caplog.at_level("INFO"):
            await mixin._apply_effect(
                _effect(background=True), _ROW, {}, {"event": event}, {}, [], [], {}
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            registry = mixin._background_run_command_registry()
            assert registry
            first_task = next(iter(registry.values()))
            cleanup_observed = asyncio.Event()
            first_task.add_done_callback(lambda _task: cleanup_observed.set())

            await mixin._apply_effect(
                _effect(background=True), _ROW, {}, {"event": event}, {}, [], [], {}
            )
            assert run.await_count == 1
            assert "suppressed duplicate background run" in caplog.text

            release.set()
            await first_task
            await asyncio.wait_for(cleanup_observed.wait(), timeout=1)
            assert registry == {}

            await mixin._apply_effect(
                _effect(background=True), _ROW, {}, {"event": event}, {}, [], [], {}
            )
            assert registry
            second_task = next(iter(registry.values()))
            await second_task
            assert run.await_count == 2


@pytest.mark.asyncio
class TestRunCommandDeadlines:
    async def test_inline_timeout_uses_remaining_aggregate_deadline(self) -> None:
        result = RunCommandResult(
            status="success",
            context=None,
            duration_ms=1.0,
            exit_code=0,
            stdout_bytes=0,
            stderr_bytes=0,
            timeout_seconds=0.1,
            overflow_stream=None,
            background=False,
            phase="execution",
            skill=None,
            script=None,
        )
        execute = AsyncMock(return_value=result)
        context_parts: list[str] = []
        with patch("gobby.workflows.engine.effects.execute_run_command", execute):
            await EffectsMixin()._apply_effect(
                _effect(timeout_seconds=5.0),
                _ROW,
                {},
                {
                    "event": _event(),
                    "_blocking_deadline": BlockingEffectDeadline(time.monotonic() + 0.2),
                },
                {},
                context_parts,
                [],
                {},
            )

        await_args = execute.await_args
        assert await_args is not None
        timeout = await_args.kwargs["timeout_seconds"]
        assert 0 < timeout <= 0.2

    async def test_exhausted_deadline_skips_spawn_and_is_audited(self) -> None:
        mixin = EffectsMixin()
        context_parts: list[str] = []
        with (
            patch(
                "gobby.workflows.engine.effects.execute_run_command", new_callable=AsyncMock
            ) as run,
            patch.object(mixin, "_audit_run_command", new_callable=AsyncMock) as audit,
        ):
            await mixin._apply_effect(
                _effect(),
                _ROW,
                {},
                {
                    "event": _event(),
                    "_blocking_deadline": BlockingEffectDeadline(time.monotonic() - 1),
                },
                {},
                context_parts,
                [],
                {},
            )

        run.assert_not_awaited()
        audit_call = audit.await_args
        assert audit_call is not None
        assert audit_call.args[0].status == "deadline_exhausted"
        assert audit_call.args[0].timeout_seconds == 0.0

    async def test_skill_resolution_timeout_fails_open_with_safe_status(self) -> None:
        mixin = EffectsMixin()

        async def wait_forever(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        mixin.skill_script_materializer = cast(
            Any,
            _materializer(AsyncMock(side_effect=wait_forever)),
        )
        with (
            patch(
                "gobby.workflows.engine.effects.execute_run_command", new_callable=AsyncMock
            ) as run,
            patch.object(mixin, "_audit_run_command", new_callable=AsyncMock) as audit,
        ):
            await mixin._apply_effect(
                _effect(
                    command=["node"],
                    skill="impeccable",
                    script="hook.mjs",
                    timeout_seconds=0.01,
                ),
                _ROW,
                {},
                {"event": _event()},
                {},
                [],
                [],
                {},
            )

        run.assert_not_awaited()
        audit_call = audit.await_args
        assert audit_call is not None
        result = audit_call.args[0]
        assert result.status == "skill_resolution_timeout"
        assert result.phase == "skill_resolution"
        assert result.skill == "impeccable"
        assert result.script == "hook.mjs"

    async def test_skill_resolution_error_logs_exception(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mixin = EffectsMixin()
        failure = ValueError("materialization denied")
        mixin.skill_script_materializer = cast(
            Any,
            _materializer(AsyncMock(side_effect=failure)),
        )

        with caplog.at_level(logging.DEBUG, logger="gobby.workflows.engine.effects"):
            result = await mixin._prepare_run_command(
                ["node"],
                project_id=None,
                skill="impeccable",
                script="hook.mjs",
                timeout=1.0,
                background=False,
            )

        assert isinstance(result, RunCommandResult)
        assert result.status == "skill_resolution_error"
        records = [
            record for record in caplog.records if "skill resolution failed" in record.message
        ]
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert records[0].exc_info[1] is failure
        record_fields = vars(records[0])
        assert record_fields["skill"] == "impeccable"
        assert record_fields["script"] == "hook.mjs"

    async def test_unexpected_skill_resolution_error_is_safe_status(self) -> None:
        mixin = EffectsMixin()
        failure = PermissionError("unexpected materializer defect")
        mixin.skill_script_materializer = cast(
            Any,
            _materializer(AsyncMock(side_effect=failure)),
        )

        result = await mixin._prepare_run_command(
            ["node"],
            project_id=None,
            skill="impeccable",
            script="hook.mjs",
            timeout=1.0,
            background=False,
        )

        assert isinstance(result, RunCommandResult)
        assert result.status == "skill_resolution_error"


@pytest.mark.asyncio
class TestRunCommandBounds:
    async def test_stdout_overflow_kills_and_reaps_child(self, tmp_path: Path) -> None:
        pid_path = tmp_path / "pid"
        script = (
            "import os, pathlib, sys, time; "
            f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
            f"sys.stdout.buffer.write(b'x' * {STDOUT_LIMIT_BYTES + 1}); "
            "sys.stdout.flush(); time.sleep(30)"
        )

        result = await execute_run_command(
            [sys.executable, "-c", script],
            cwd=str(tmp_path),
            stdin_payload=b"{}",
            timeout_seconds=5,
            background=False,
        )

        assert result.status == "output_limit"
        assert result.overflow_stream == "stdout"
        assert result.stdout_bytes > STDOUT_LIMIT_BYTES
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)

    async def test_stderr_overflow_is_capped_independently(self, tmp_path: Path) -> None:
        result = await execute_run_command(
            [
                sys.executable,
                "-c",
                f"import sys; sys.stderr.buffer.write(b'x' * {STDERR_LIMIT_BYTES + 1})",
            ],
            cwd=str(tmp_path),
            stdin_payload=b"{}",
            timeout_seconds=5,
            background=False,
        )

        assert result.status == "output_limit"
        assert result.overflow_stream == "stderr"
        assert result.stderr_bytes > STDERR_LIMIT_BYTES

    async def test_timeout_kills_and_reaps_child(self, tmp_path: Path) -> None:
        pid_path = tmp_path / "pid"
        script = (
            "import os, pathlib, time; "
            f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
            "time.sleep(30)"
        )

        result = await execute_run_command(
            [sys.executable, "-c", script],
            cwd=str(tmp_path),
            stdin_payload=b"{}",
            timeout_seconds=0.2,
            background=False,
        )

        assert result.status == "timeout"
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)


def test_run_command_payload_preserves_provider_fields_and_normalizes_edits() -> None:
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="provider-session",
        source=SessionSource.GROK,
        timestamp=datetime.now(UTC),
        data={
            "function_name": "Write",
            "parameters": {
                "file_path": "one.tsx",
                "file_paths": ["one.tsx", "two.tsx"],
            },
            "provider_field": {"nested": True},
        },
        cwd="/tmp/project",
        metadata={},
    )

    payload = build_run_command_payload(event)

    assert payload["function_name"] == "Write"
    assert payload["parameters"] == {
        "file_path": "one.tsx",
        "file_paths": ["one.tsx", "two.tsx"],
    }
    assert payload["tool_input"] == payload["parameters"]
    assert payload["provider_field"] == {"nested": True}
    assert payload["cwd"] == "/tmp/project"
    assert payload["hook_event_name"] == "PostToolUse"
    assert "tool_name" not in event.data


def test_run_command_payload_synthesizes_stop_only_when_missing() -> None:
    stop_event = _event(hook_event_name="ProviderStop")
    stop_event.event_type = HookEventType.STOP
    preserved = build_run_command_payload(stop_event)
    stop_event.data.pop("hook_event_name")
    synthesized = build_run_command_payload(stop_event)

    assert preserved["hook_event_name"] == "ProviderStop"
    assert synthesized["hook_event_name"] == "Stop"


def test_resolve_materialized_skill_script_returns_contained_file(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    expected = scripts_dir / "hook.mjs"
    expected.write_text("export {};\n")

    resolved = resolve_materialized_skill_script(scripts_dir, "hook.mjs")

    assert resolved == expected


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("", "Skill script path must be non-empty"),
        ("/tmp/hook.mjs", "Skill script path must be relative"),
        ("../other/hook.mjs", "Skill script path cannot traverse its scripts directory"),
        (
            "nested/../../hook.mjs",
            "Skill script path cannot traverse its scripts directory",
        ),
        (r"C:\temp\hook.mjs", "Skill script path must be relative"),
    ],
)
def test_resolve_materialized_skill_script_rejects_unsafe_path(
    script: str,
    message: str,
    tmp_path: Path,
) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    with pytest.raises(ValueError) as exc_info:
        resolve_materialized_skill_script(scripts_dir, script)
    assert str(exc_info.value) == message


def test_resolve_materialized_skill_script_rejects_missing_file(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    with pytest.raises(ValueError) as exc_info:
        resolve_materialized_skill_script(scripts_dir, "missing.mjs")
    assert str(exc_info.value) == "Materialized skill script path could not be resolved"
    assert isinstance(exc_info.value.__cause__, FileNotFoundError)


def test_resolve_materialized_skill_script_wraps_root_resolution_failure(
    tmp_path: Path,
) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    with (
        patch.object(Path, "resolve", side_effect=PermissionError("denied")),
        pytest.raises(ValueError) as exc_info,
    ):
        resolve_materialized_skill_script(scripts_dir, "hook.mjs")

    assert str(exc_info.value) == "Materialized skill script path could not be resolved"
    assert isinstance(exc_info.value.__cause__, PermissionError)


def test_resolve_materialized_skill_script_preserves_directory_policy_message(
    tmp_path: Path,
) -> None:
    scripts_path = tmp_path / "scripts.mjs"
    scripts_path.write_text("export {};\n")

    with pytest.raises(ValueError) as exc_info:
        resolve_materialized_skill_script(scripts_path, "hook.mjs")

    assert str(exc_info.value) == "Materialized scripts path is not a directory"


def test_resolve_materialized_skill_script_preserves_file_policy_message(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    target_dir = scripts_dir / "nested"
    target_dir.mkdir(parents=True)

    with pytest.raises(ValueError) as exc_info:
        resolve_materialized_skill_script(scripts_dir, "nested")

    assert str(exc_info.value) == "Skill script path must name a file"


def test_resolve_materialized_skill_script_rejects_symlink_escape(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    outside = tmp_path / "outside.mjs"
    outside.write_text("export {};\n")
    (scripts_dir / "hook.mjs").symlink_to(outside)

    with pytest.raises(ValueError, match="outside"):
        resolve_materialized_skill_script(scripts_dir, "hook.mjs")


@pytest.mark.parametrize(
    ("command", "timeout", "message"),
    [
        ([], None, "non-empty command"),
        (["true"], 0.0, "must be > 0"),
        (["true"], -1.0, "must be > 0"),
    ],
)
def test_run_command_effect_rejects_invalid_bounds(
    command: list[str], timeout: float | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        RuleEffect(type="run_command", command=command, timeout_seconds=timeout)


@pytest.mark.parametrize(
    "fields",
    [
        {"skill": "impeccable"},
        {"script": "hook.mjs"},
    ],
)
def test_run_command_effect_requires_skill_and_script_together(fields: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="must be provided together"):
        RuleEffect(type="run_command", command=["node"], **fields)


@pytest.mark.parametrize("script", ["", "/tmp/hook.mjs", "../hook.mjs", r"C:\hook.mjs"])
def test_run_command_effect_rejects_unsafe_script(script: str) -> None:
    with pytest.raises(ValueError, match="non-empty|relative|traverse"):
        RuleEffect(
            type="run_command",
            command=["node"],
            skill="impeccable",
            script=script,
        )


@pytest.mark.parametrize(
    "status",
    [
        "success",
        "spawn_error",
        "nonzero_exit",
        "timeout",
        "output_limit",
        "invalid_output",
        "deadline_exhausted",
        "skill_resolution_error",
        "skill_resolution_timeout",
    ],
)
@pytest.mark.asyncio
async def test_run_command_audit_contains_metadata_only(status: str) -> None:
    mixin = EffectsMixin()
    mixin.db = MagicMock()
    manager = MagicMock()
    result = RunCommandResult(
        status=cast(Any, status),
        context="secret detector output",
        duration_ms=12.5,
        exit_code=3,
        stdout_bytes=44,
        stderr_bytes=55,
        timeout_seconds=5.0,
        overflow_stream="stdout" if status == "output_limit" else None,
        background=False,
        phase="skill_resolution" if status.startswith("skill_resolution") else "execution",
        skill="impeccable",
        script="hook.mjs",
    )
    with patch("gobby.storage.workflow_audit.WorkflowAuditManager", return_value=manager):
        await mixin._audit_run_command(
            result,
            rule_name="test-rule",
            rule_id="rule-id",
            platform_session_id="platform-session-1",
        )

    kwargs = manager.log.call_args.kwargs
    assert kwargs["event_type"] == "effect"
    assert kwargs["result"] == status
    assert kwargs["rule_id"] == "rule-id"
    assert kwargs["context"] == {
        "phase": "skill_resolution" if status.startswith("skill_resolution") else "execution",
        "skill": "impeccable",
        "script": "hook.mjs",
        "duration_ms": 12.5,
        "exit_code": 3,
        "stdout_bytes": 44,
        "stderr_bytes": 55,
        "timeout_seconds": 5.0,
        "overflow_stream": "stdout" if status == "output_limit" else None,
        "background": False,
    }
    assert "secret detector output" not in json.dumps(kwargs)


class TestParseCommandOutput:
    def test_hook_specific_output_shape(self) -> None:
        payload = json.dumps({"hookSpecificOutput": {"additionalContext": "finding"}}).encode()
        assert _parse_command_output(payload) == (True, "finding")

    def test_top_level_additional_context(self) -> None:
        payload = json.dumps({"additionalContext": "note"}).encode()
        assert _parse_command_output(payload) == (True, "note")

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            (b"", (True, None)),
            (b"not json", (False, None)),
            (b"[]", (False, None)),
            (b"42", (False, None)),
            (json.dumps({"hookSpecificOutput": {}}).encode(), (True, None)),
            (b"{}", (True, None)),
        ],
    )
    def test_output_validity_is_distinct_from_missing_context(
        self,
        payload: bytes,
        expected: tuple[bool, str | None],
    ) -> None:
        assert _parse_command_output(payload) == expected
