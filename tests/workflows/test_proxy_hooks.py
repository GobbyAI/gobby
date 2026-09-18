from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from gobby.hooks.effect_deadline import (
    BLOCKING_EFFECT_BUDGET_SECONDS,
    BlockingEffectDeadline,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.integrations.rtk import RTK_RULE_NAME, RtkProbe, clear_probe_cache
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine import proxy_hooks
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit

SESSION_ID = "proxy-hook-test-session"
RTK_RULE_PATH = (
    Path(__file__).parents[2]
    / "src/gobby/install/shared/workflows/rules/integrations/rtk-command-rewrite.yaml"
)


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


@pytest.fixture(autouse=True)
def _reset_rtk_state(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_probe_cache()
    monkeypatch.setattr(proxy_hooks, "_rtk_unavailable_warned", False)


@pytest.fixture
def fake_rtk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Mimic RTK resolution and subprocess behavior without cold script execution."""
    executable = tmp_path / "rtk"
    executable.touch()
    monkeypatch.setenv("GOBBY_RTK_BIN", str(executable))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))

    probe = RtkProbe(path=executable, version="0.45.0", compatible=True)

    def resolve_fake_rtk(*, timeout: float) -> RtkProbe | None:
        del timeout
        if os.environ.get("GOBBY_RTK_BIN") != str(executable):
            return None
        return probe

    class FakeProcess:
        def __init__(self, *, code: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
            self._code = code
            self._delay = 2.0 if os.environ.get("FAKE_RTK_MODE") == "sleep" else 0.0
            self.returncode: int | None = None
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(stdout)
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(stderr)
            self.stderr.feed_eof()

        async def wait(self) -> int:
            if self.returncode is not None:
                return self.returncode
            if self._delay:
                await asyncio.sleep(self._delay)
            self.returncode = self._code
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    async def create_fake_subprocess(*args: str, **kwargs: object) -> FakeProcess:
        del kwargs
        assert args[:3] == (str(executable), "rewrite", "--")
        command = args[3]
        argv_log = os.environ.get("FAKE_RTK_ARGV_LOG")
        if argv_log:
            Path(argv_log).write_text(
                json.dumps(list(args[1:])) + "\n",
                encoding="utf-8",
            )

        mode = os.environ.get("FAKE_RTK_MODE", "rewrite")
        if mode == "pass":
            return FakeProcess(code=1)
        if mode == "deny":
            return FakeProcess(code=2)
        if mode == "ask":
            return FakeProcess(code=3, stdout=f"rtk {command}".encode())
        if mode == "runtime_error":
            return FakeProcess(code=0, stdout=b"[rtk: No such file or directory (os error 2)]")
        if mode == "stderr_error":
            return FakeProcess(
                code=0,
                stdout=f"rtk {command}".encode(),
                stderr=b"rtk: rewrite failed",
            )
        if mode in {"unsupported_jq", "keep_context"}:
            return FakeProcess(code=3, stdout=os.environ["FAKE_RTK_REWRITE"].encode())
        if mode == "invalid":
            return FakeProcess(code=0, stdout=b"\xff")
        if mode == "oversized":
            return FakeProcess(code=0, stdout=b"x" * (70 * 1024))
        if mode == "unexpected":
            return FakeProcess(code=7)
        return FakeProcess(code=0, stdout=f"rtk {command}".encode())

    monkeypatch.setattr(proxy_hooks, "resolve_rtk", resolve_fake_rtk)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_fake_subprocess)
    return executable


def _event(
    command: str = "git status",
    *,
    source: SessionSource = SessionSource.CLAUDE,
    cwd: str | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=source,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Bash", "tool_input": {"command": command}},
        cwd=cwd,
    )


def _linked_worktree(root: Path, name: str) -> Path:
    """Create a linked worktree of ``root/main`` whose ``.git`` file points at its git dir."""
    git_dir = root / "main" / ".git" / "worktrees" / name
    git_dir.mkdir(parents=True)
    worktree = root / "worktrees" / name
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    return worktree


def _create_rule(
    manager: RuleDefinitionManager,
    name: str,
    effects: list[RuleEffect],
    *,
    priority: int,
    when: str | None = None,
) -> None:
    body = RuleDefinitionBody(
        event=RuleTriggerEvent.BEFORE_TOOL,
        effects=effects,
        when=when,
    )
    manager.create(
        name=name,
        definition_json=body.model_dump_json(),
        priority=priority,
        enabled=True,
    )


def _proxy_effect(**kwargs: Any) -> RuleEffect:
    return RuleEffect(type="proxy_hook", handler="rtk", **kwargs)


def _record_rtk_outcomes(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Capture every _run_rtk_proxy return value so skip behavior stays assertable."""
    outcomes: list[bool] = []
    original = proxy_hooks.ProxyHooksMixin._run_rtk_proxy

    async def _record(
        mixin: proxy_hooks.ProxyHooksMixin,
        invocation: proxy_hooks.ProxyHookInvocation,
        event: HookEvent,
        *,
        blocking_deadline: BlockingEffectDeadline | None,
    ) -> bool:
        outcome = await original(mixin, invocation, event, blocking_deadline=blocking_deadline)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(proxy_hooks.ProxyHooksMixin, "_run_rtk_proxy", _record)
    return outcomes


def _parse_overrun_warning(
    caplog: pytest.LogCaptureFixture,
    *,
    rule_name: str,
    stage: str,
    floor: float,
) -> float:
    """Return the spent-budget figure from the one overrun warning emitted."""
    pattern = re.compile(
        rf"proxy_hook\[{re.escape(rule_name)}\]: blocking budget spent "
        rf"{re.escape(stage)}, running on the {floor:.1f}s floor "
        rf"\(shared budget (?P<spent>\d+\.\d)s of "
        rf"{BLOCKING_EFFECT_BUDGET_SECONDS:.1f}s spent\)"
    )
    matches = [
        match for message in caplog.messages if (match := pattern.fullmatch(message)) is not None
    ]
    assert len(matches) == 1, caplog.messages
    return float(matches[0].group("spent"))


@pytest.mark.parametrize(
    ("unsafe_field", "value"),
    [
        ("background", True),
        ("command", ["echo", "unsafe"]),
        ("inject_result", True),
        ("permission_decision", "allow"),
        ("auto_approve", True),
    ],
)
def test_proxy_effect_contract_rejects_unsafe_shapes(
    unsafe_field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=f"forbids: {unsafe_field}"):
        RuleEffect.model_validate({"type": "proxy_hook", "handler": "rtk", unsafe_field: value})


def test_proxy_effect_contract_rejects_non_before_tool_event() -> None:
    with pytest.raises(ValueError, match="restricted to before_tool"):
        RuleDefinitionBody(
            event=RuleTriggerEvent.AFTER_TOOL,
            effects=[_proxy_effect()],
        )


def test_bundled_rtk_rule_is_disabled_and_permission_neutral() -> None:
    document = yaml.safe_load(RTK_RULE_PATH.read_text(encoding="utf-8"))
    definition = document["rules"]["rtk-command-rewrite"]

    assert definition["enabled"] is False
    assert definition["priority"] == 90
    assert "default" in document["tags"]
    assert definition["event"] == "before_tool"
    assert definition["tools"] == ["Bash"]
    assert definition["effects"] == [{"type": "proxy_hook", "handler": "rtk", "timeout_seconds": 2}]
    RuleDefinitionBody.model_validate(
        {key: value for key, value in definition.items() if key in {"event", "tools", "effects"}}
    )


def test_bundled_rtk_rule_syncs_as_disabled_installed_row(
    db: HubDatabase,
    manager: RuleDefinitionManager,
) -> None:
    result = sync_bundled_rules(db, get_bundled_rules_path())

    assert result["errors"] == []
    row = manager.get_by_name(RTK_RULE_NAME, project_id=None)
    assert row is not None
    assert row.source == "installed"
    assert row.enabled is False
    assert row.priority == 90
    # Agent rule selectors activate rules by tag (every bundled agent includes
    # "tag:default"); without it the engine drops the rule before evaluation.
    assert "default" in (row.tags or [])
    body = RuleDefinitionBody.model_validate(row.definition_json)
    assert body.group == "integrations"
    assert body.tools == ["Bash"]
    assert body.effects is not None
    [effect] = body.effects
    assert (effect.type, effect.handler, effect.timeout_seconds) == ("proxy_hook", "rtk", 2)


async def test_rtk_rewrite_is_permission_neutral(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
) -> None:
    _create_rule(manager, "rtk", [_proxy_effect()], priority=90)

    response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert response.modified_input == {"command": "rtk git status"}
    assert response.permission_decision is None


@pytest.mark.parametrize(
    "rewrite",
    [
        "rtk jq empty manifest.json",
        "uv run rtk jq empty manifest.json && rtk ruff check script.py",
        "command rtk jq empty manifest.json",
        "rtk /usr/bin/jq empty manifest.json",
        "X=1 rtk jq empty manifest.json",
        "env X=1 rtk jq empty manifest.json",
        "sudo rtk jq empty manifest.json",
    ],
)
async def test_unsupported_rtk_jq_rewrite_falls_back_to_original_command(
    rewrite: str,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_RTK_MODE", "unsupported_jq")
    monkeypatch.setenv("FAKE_RTK_REWRITE", rewrite)
    _create_rule(manager, "rtk", [_proxy_effect()], priority=90)

    response = await RuleEngine(db).evaluate(
        _event("jq empty manifest.json"),
        SESSION_ID,
        {},
    )

    assert response.modified_input is None
    assert response.permission_decision is None


async def test_rtk_rewrite_logs_at_debug(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful rewrite is routine, so it stays out of the INFO log (#20981)."""
    _create_rule(manager, "rtk", [_proxy_effect()], priority=90)

    with caplog.at_level(logging.DEBUG, logger=proxy_hooks.logger.name):
        response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert response.modified_input == {"command": "rtk git status"}
    transformed = [
        record for record in caplog.records if "RTK transformed command" in record.getMessage()
    ]
    assert [record.levelno for record in transformed] == [logging.DEBUG]


async def test_rtk_ask_rewrite_returns_the_complete_tool_input(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude applies ``updatedInput`` wholesale, so the rewrite carries every field."""
    monkeypatch.setenv("FAKE_RTK_MODE", "ask")
    _create_rule(manager, "rtk", [_proxy_effect()], priority=90)
    event = _event()
    event.data["tool_input"] = {
        "command": "git status",
        "timeout": 300000,
        "description": "Show status",
        "run_in_background": False,
    }

    response = await RuleEngine(db).evaluate(event, SESSION_ID, {})

    assert response.modified_input == {
        "command": "rtk git status",
        "timeout": 300000,
        "description": "Show status",
        "run_in_background": False,
    }
    assert response.permission_decision is None
    assert response.auto_approve is False


async def test_original_block_skips_proxy_handlers(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def record_proxy(*args: object, **kwargs: object) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(RuleEngine, "_run_proxy_hooks", record_proxy)
    _create_rule(manager, "proxy", [_proxy_effect()], priority=10)
    _create_rule(
        manager,
        "block-original",
        [RuleEffect(type="block", reason="blocked")],
        priority=20,
    )

    response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert response.decision == "block"
    assert calls == 0


async def test_prior_rewrite_composes_before_proxy(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
) -> None:
    _create_rule(
        manager,
        "declarative",
        [RuleEffect(type="rewrite_input", input_updates={"command": "git log -1"})],
        priority=10,
    )
    _create_rule(manager, "proxy", [_proxy_effect()], priority=20)

    response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert response.modified_input == {"command": "rtk git log -1"}


async def test_sequential_proxy_handlers_receive_latest_input(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def transform(
        self: RuleEngine,
        invocation: object,
        event: HookEvent,
        *,
        blocking_deadline: BlockingEffectDeadline | None,
    ) -> bool:
        del self, invocation, blocking_deadline
        tool_input = event.data["tool_input"]
        command = tool_input["command"]
        seen.append(command)
        tool_input["command"] = f"wrapped({command})"
        return True

    monkeypatch.setattr(RuleEngine, "_run_rtk_proxy", transform)
    _create_rule(manager, "first", [_proxy_effect()], priority=10)
    _create_rule(manager, "second", [_proxy_effect()], priority=20)

    response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert seen == ["git status", "wrapped(git status)"]
    assert response.modified_input == {"command": "wrapped(wrapped(git status))"}


async def test_final_transformed_input_is_reblocked(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
) -> None:
    _create_rule(manager, "proxy", [_proxy_effect()], priority=10)
    _create_rule(
        manager,
        "block-final",
        [
            RuleEffect(
                type="block",
                reason="wrapped commands are forbidden",
                command_pattern=r"^rtk git status$",
            )
        ],
        priority=20,
    )

    variables: dict[str, Any] = {}
    response = await RuleEngine(db).evaluate(_event(), SESSION_ID, variables)

    assert response.decision == "block"
    assert "block-final" in (response.reason or "")
    assert response.modified_input is None
    assert "_rewrite_input" not in variables


@pytest.mark.parametrize(
    "gate_name",
    ["_check_agent_tool_enforcement", "_check_step_tool_enforcement"],
)
async def test_final_transform_rechecks_enforcement_gates(
    gate_name: str,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def block_transformed(
        self: RuleEngine,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
    ) -> HookResponse | None:
        del self, session_id, variables
        tool_input = event.data.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if command == "rtk git status":
            return HookResponse(decision="block", reason=f"{gate_name} blocked final input")
        return None

    monkeypatch.setattr(RuleEngine, gate_name, block_transformed)
    _create_rule(manager, f"proxy-{gate_name}", [_proxy_effect()], priority=10)

    variables: dict[str, Any] = {}
    response = await RuleEngine(db).evaluate(_event(), SESSION_ID, variables)

    assert response.decision == "block"
    assert response.reason == f"{gate_name} blocked final input"
    assert response.modified_input is None
    assert "_rewrite_input" not in variables


async def test_stale_rewrite_does_not_leak_into_next_event(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def block_transformed(
        self: RuleEngine,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
    ) -> HookResponse | None:
        del self, session_id, variables
        tool_input = event.data.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if command == "rtk git status":
            return HookResponse(decision="block", reason="blocked final input")
        return None

    monkeypatch.setattr(RuleEngine, "_check_agent_tool_enforcement", block_transformed)
    _create_rule(manager, "proxy", [_proxy_effect()], priority=10)
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    blocked = await engine.evaluate(_event(), SESSION_ID, variables)
    unrelated = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}},
    )
    following = await engine.evaluate(unrelated, SESSION_ID, variables)

    assert blocked.decision == "block"
    assert following.decision == "allow"
    assert following.modified_input is None


async def test_unknown_handler_fails_open(
    db: HubDatabase,
    manager: RuleDefinitionManager,
) -> None:
    _create_rule(
        manager,
        "unknown",
        [RuleEffect(type="proxy_hook", handler="missing")],
        priority=10,
    )

    response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert response.decision == "allow"
    assert response.modified_input is None


@pytest.mark.parametrize("mode", ["pass", "deny", "unexpected"])
async def test_rtk_nonzero_exit_falls_back_to_original_command(
    mode: str,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_RTK_MODE", mode)
    _create_rule(manager, f"proxy-{mode}", [_proxy_effect()], priority=10)

    response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert response.modified_input is None


@pytest.mark.parametrize(
    ("command", "rewrite"),
    [
        pytest.param(
            "DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
            " GOBBY_TEST_PROTECT=1 uv run pytest tests/x.py -q",
            "DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
            " GOBBY_TEST_PROTECT=1 uv run rtk pytest tests/x.py -q",
            id="env-assignments",
        ),
        pytest.param(
            'A="x y" B=2 cargo test -p gobby-core',
            'A="x y" B=2 rtk cargo test -p gobby-core',
            id="quoted-assignment",
        ),
        pytest.param(
            'X=/some/path; git -C "$X" status',
            'X=/some/path; rtk git -C "$X" status',
            id="assignment-statement",
        ),
        pytest.param(
            "cd /some/path && git status",
            "cd /some/path && rtk git status",
            id="leading-cd",
        ),
        pytest.param("cdk deploy --all", "rtk cdk deploy --all", id="cd-prefixed-word"),
    ],
)
async def test_rtk_rewrite_applies_when_the_shell_context_is_kept(
    command: str,
    rewrite: str,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_RTK_MODE", "keep_context")
    monkeypatch.setenv("FAKE_RTK_REWRITE", rewrite)
    _create_rule(manager, "proxy-context", [_proxy_effect()], priority=10)

    response = await RuleEngine(db).evaluate(_event(command), SESSION_ID, {})

    assert response.modified_input == {"command": rewrite}
    assert response.permission_decision is None


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('X=/some/path; git -C "$X" status', id="assignment-statement"),
        pytest.param("GOBBY_TEST_PROTECT=1 uv run pytest tests/x.py", id="env-assignment"),
        pytest.param("cd /some/path && git status", id="leading-cd"),
    ],
)
async def test_rtk_rewrite_that_detaches_the_shell_context_falls_back(
    command: str,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
) -> None:
    # The default fake prepends the launcher, which moves ``rtk`` in front of the
    # assignments or ``cd`` and would run the command in another environment.
    _create_rule(manager, "proxy-context", [_proxy_effect()], priority=10)

    response = await RuleEngine(db).evaluate(_event(command), SESSION_ID, {})

    assert response.modified_input is None


@pytest.mark.parametrize(
    ("command", "rewrite"),
    [
        pytest.param(
            "npx vitest run tests/workflows/x.test.ts",
            "rtk vitest tests/workflows/x.test.ts",
            id="npx-vitest-run",
        ),
        pytest.param(
            "pnpm exec vitest run tests/workflows/x.test.ts",
            "rtk vitest tests/workflows/x.test.ts",
            id="pnpm-exec-vitest-run",
        ),
        pytest.param(
            "npm exec vitest run tests/workflows/x.test.ts",
            "rtk vitest tests/workflows/x.test.ts",
            id="npm-exec-vitest-run",
        ),
        pytest.param("npx tsc --noEmit", "rtk tsc --noEmit", id="npx-tsc"),
        pytest.param(
            "cd web && npx vitest run src/hooks",
            "cd web && rtk vitest src/hooks",
            id="cd-prefixed-npx-vitest-run",
        ),
        pytest.param(
            "GOBBY_TEST_PROTECT=1 npx vitest run tests/workflows/x.test.ts",
            "GOBBY_TEST_PROTECT=1 rtk vitest tests/workflows/x.test.ts",
            id="env-prefixed-npx-vitest-run",
        ),
        pytest.param(
            "npx tsc --noEmit && npx vitest run tests/workflows/x.test.ts",
            "npx rtk tsc --noEmit && rtk vitest tests/workflows/x.test.ts",
            id="second-segment-launcher-dropped",
        ),
    ],
)
async def test_rtk_rewrite_that_drops_a_package_manager_launcher_falls_back(
    command: str,
    rewrite: str,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_RTK_MODE", "keep_context")
    monkeypatch.setenv("FAKE_RTK_REWRITE", rewrite)
    _create_rule(manager, "proxy-launcher", [_proxy_effect()], priority=10)

    response = await RuleEngine(db).evaluate(_event(command), SESSION_ID, {})

    assert response.modified_input is None


@pytest.mark.parametrize(
    ("command", "rewrite"),
    [
        pytest.param(
            "npx vitest run tests/workflows/x.test.ts",
            "npx rtk vitest tests/workflows/x.test.ts",
            id="npx-vitest-run",
        ),
        pytest.param(
            "cd web && npx vitest run src/hooks",
            "cd web && npx rtk vitest src/hooks",
            id="cd-prefixed-npx-vitest-run",
        ),
        pytest.param(
            "GOBBY_TEST_PROTECT=1 npx vitest run tests/workflows/x.test.ts",
            "GOBBY_TEST_PROTECT=1 npx rtk vitest tests/workflows/x.test.ts",
            id="env-prefixed-npx-vitest-run",
        ),
        pytest.param(
            "npx tsc --noEmit && npx vitest run tests/workflows/x.test.ts",
            "npx rtk tsc --noEmit && npx rtk vitest tests/workflows/x.test.ts",
            id="every-segment-keeps-its-launcher",
        ),
        # RTK 0.49.0 wraps a launcher it does not special-case, so the launcher
        # survives inside the wrapper rather than ahead of it.
        pytest.param(
            "bunx vitest run tests/x.test.ts",
            "rtk bunx vitest run tests/x.test.ts",
            id="bunx-wrapped-by-rtk",
        ),
        pytest.param(
            "npx vitest run tests/x.test.ts",
            "rtk npx vitest run tests/x.test.ts",
            id="npx-wrapped-by-rtk",
        ),
        pytest.param(
            "pnpm install",
            "rtk pnpm install",
            id="pnpm-install-wrapped-by-rtk",
        ),
        pytest.param(
            "pnpm vitest run tests/x.test.ts",
            "pnpm rtk vitest tests/x.test.ts",
            id="pnpm",
        ),
        pytest.param(
            "pnpm exec vitest run tests/x.test.ts",
            "pnpm exec rtk vitest tests/x.test.ts",
            id="pnpm-exec",
        ),
        pytest.param(
            "pnpm dlx some-cli --check",
            "pnpm dlx rtk some-cli --check",
            id="pnpm-dlx",
        ),
        pytest.param(
            "npm exec tsc --noEmit",
            "npm exec rtk tsc --noEmit",
            id="npm-exec",
        ),
        pytest.param(
            "yarn vitest run tests/x.test.ts",
            "yarn rtk vitest tests/x.test.ts",
            id="yarn",
        ),
        pytest.param(
            "yarn dlx some-cli --check",
            "yarn dlx rtk some-cli --check",
            id="yarn-dlx",
        ),
        pytest.param(
            "bun x vitest run tests/x.test.ts",
            "rtk bun x vitest run tests/x.test.ts",
            id="bun-x-wrapped-by-rtk",
        ),
    ],
)
async def test_rtk_rewrite_that_keeps_a_package_manager_launcher_applies(
    command: str,
    rewrite: str,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_RTK_MODE", "keep_context")
    monkeypatch.setenv("FAKE_RTK_REWRITE", rewrite)
    _create_rule(manager, "proxy-launcher", [_proxy_effect()], priority=10)

    response = await RuleEngine(db).evaluate(_event(command), SESSION_ID, {})

    assert response.modified_input == {"command": rewrite}


@pytest.mark.parametrize("command", ["git add -A", "git commit -m wip"])
async def test_claude_git_stays_bare_in_linked_worktree(
    command: str,
    tmp_path: Path,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
) -> None:
    worktree = _linked_worktree(tmp_path, "wt-1")
    _create_rule(manager, "rtk", [_proxy_effect()], priority=90)

    response = await RuleEngine(db).evaluate(
        _event(command, cwd=str(worktree)),
        SESSION_ID,
        {},
    )

    assert response.modified_input is None


async def test_claude_git_dash_c_other_worktree_stays_bare_in_linked_worktree(
    tmp_path: Path,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
) -> None:
    worktree = _linked_worktree(tmp_path, "wt-1")
    other = _linked_worktree(tmp_path, "wt-2")
    _create_rule(manager, "rtk", [_proxy_effect()], priority=90)

    response = await RuleEngine(db).evaluate(
        _event(f"git -C {other} status", cwd=str(worktree)),
        SESSION_ID,
        {},
    )

    assert response.modified_input is None


async def test_claude_non_git_rewrite_still_applies_in_linked_worktree(
    tmp_path: Path,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
) -> None:
    worktree = _linked_worktree(tmp_path, "wt-1")
    command = "uv run pytest tests/workflows/test_proxy_hooks.py"
    _create_rule(manager, "rtk", [_proxy_effect()], priority=90)

    response = await RuleEngine(db).evaluate(
        _event(command, cwd=str(worktree)),
        SESSION_ID,
        {},
    )

    assert response.modified_input == {"command": f"rtk {command}"}


async def test_claude_git_rewrite_still_applies_in_main_checkout(
    tmp_path: Path,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
) -> None:
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    _create_rule(manager, "rtk", [_proxy_effect()], priority=90)

    response = await RuleEngine(db).evaluate(
        _event("git status", cwd=str(main)),
        SESSION_ID,
        {},
    )

    assert response.modified_input == {"command": "rtk git status"}


async def test_other_provider_git_rewrite_still_applies_in_linked_worktree(
    tmp_path: Path,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
) -> None:
    worktree = _linked_worktree(tmp_path, "wt-1")
    _create_rule(manager, "rtk", [_proxy_effect()], priority=90)

    response = await RuleEngine(db).evaluate(
        _event("git status", source=SessionSource.CODEX, cwd=str(worktree)),
        SESSION_ID,
        {},
    )

    assert response.modified_input == {"command": "rtk git status"}


@pytest.mark.parametrize(
    "mode, detail",
    [
        ("runtime_error", "No such file or directory"),
        ("stderr_error", "rewrite failed"),
    ],
)
async def test_rtk_error_output_falls_back_to_original_command(
    mode: str,
    detail: str,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("FAKE_RTK_MODE", mode)
    _create_rule(manager, f"proxy-{mode}", [_proxy_effect()], priority=10)

    with caplog.at_level(logging.DEBUG, logger=proxy_hooks.logger.name):
        response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert response.modified_input is None
    messages = [record.getMessage() for record in caplog.records]
    assert any(detail in message for message in messages)


@pytest.mark.parametrize("mode", ["pass", "deny"])
async def test_rtk_passthrough_verdicts_are_silent(
    mode: str,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("FAKE_RTK_MODE", mode)
    _create_rule(manager, f"proxy-{mode}", [_proxy_effect()], priority=10)

    with caplog.at_level(logging.WARNING, logger=proxy_hooks.logger.name):
        response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert response.decision == "allow"
    assert response.modified_input is None
    assert not [record for record in caplog.records if record.name == proxy_hooks.logger.name]


@pytest.mark.parametrize("mode", ["invalid", "oversized"])
async def test_rtk_failures_pass_through(
    mode: str,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("FAKE_RTK_MODE", mode)
    _create_rule(manager, f"proxy-{mode}", [_proxy_effect()], priority=10)

    with caplog.at_level(logging.WARNING, logger=proxy_hooks.logger.name):
        response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert response.decision == "allow"
    assert response.modified_input is None
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert f"proxy-{mode}" in warnings[0].getMessage()


async def test_unavailable_rtk_warns_once_per_episode(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("GOBBY_RTK_BIN", str(tmp_path / "missing-rtk"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    _create_rule(manager, "proxy", [_proxy_effect()], priority=10)
    engine = RuleEngine(db)

    with caplog.at_level(logging.DEBUG, logger=proxy_hooks.logger.name):
        await engine.evaluate(_event(), SESSION_ID, {})
        await engine.evaluate(_event(), SESSION_ID, {})
        monkeypatch.setenv("GOBBY_RTK_BIN", str(fake_rtk))
        recovered = await engine.evaluate(_event(), SESSION_ID, {})
        monkeypatch.setenv("GOBBY_RTK_BIN", str(tmp_path / "missing-rtk"))
        await engine.evaluate(_event(), SESSION_ID, {})

    assert recovered.modified_input == {"command": "rtk git status"}
    unavailable = [
        record.levelno for record in caplog.records if "unavailable" in record.getMessage()
    ]
    assert unavailable == [logging.WARNING, logging.DEBUG, logging.WARNING]


async def test_rtk_timeout_passes_through(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("FAKE_RTK_MODE", "sleep")
    # 1.0s leaves the resolver a 250ms probe budget (enough for the Python fake
    # to answer --version) while the fake's 2s sleep still overruns the rewrite.
    _create_rule(manager, "proxy-timeout", [_proxy_effect(timeout_seconds=1.0)], priority=10)

    with caplog.at_level(logging.WARNING, logger=proxy_hooks.logger.name):
        response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert response.decision == "allow"
    assert response.modified_input is None
    assert [
        record.getMessage() for record in caplog.records if record.name == proxy_hooks.logger.name
    ] == ["proxy_hook[proxy-timeout]: RTK timed out"]


async def test_spent_shared_budget_still_runs_rtk_on_its_floor(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The proxy stage runs last, so a shared budget with a zero floor starves it alone.

    Its declared timeout is a reservation: the rewrite still applies, and the
    spent budget is reported instead of silently dropping it.
    """
    del fake_rtk
    _create_rule(manager, "proxy-deadline", [_proxy_effect()], priority=10)
    outcomes = _record_rtk_outcomes(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=proxy_hooks.logger.name):
        response = await RuleEngine(db).evaluate(
            _event(),
            SESSION_ID,
            {},
            blocking_deadline=BlockingEffectDeadline(time.monotonic() - 1.0),
        )

    assert outcomes == [True]
    assert response.modified_input == {"command": "rtk git status"}
    spent = _parse_overrun_warning(
        caplog,
        rule_name="proxy-deadline",
        stage="before the RTK probe",
        floor=2.0,
    )
    assert spent > BLOCKING_EFFECT_BUDGET_SECONDS


async def test_deadline_exhausted_after_the_probe_names_that_site(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The probe itself can spend the last of the budget, and that reads differently.

    Both sites used to emit one indistinguishable message, so a log full of them
    said nothing about where the budget went.
    """
    _create_rule(manager, "proxy-late-deadline", [_proxy_effect()], priority=10)
    deadline = BlockingEffectDeadline(time.monotonic() + BLOCKING_EFFECT_BUDGET_SECONDS)

    def _resolve_then_exhaust(*, timeout: float) -> RtkProbe | None:
        """Spend the rest of the budget inside the probe, as a slow probe would."""
        del timeout
        deadline.expires_at = time.monotonic() - 1.0
        return RtkProbe(path=fake_rtk, version="0.45.0", compatible=True)

    monkeypatch.setattr(proxy_hooks, "resolve_rtk", _resolve_then_exhaust)
    outcomes = _record_rtk_outcomes(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=proxy_hooks.logger.name):
        response = await RuleEngine(db).evaluate(
            _event(),
            SESSION_ID,
            {},
            blocking_deadline=deadline,
        )

    assert outcomes == [True]
    assert response.modified_input == {"command": "rtk git status"}
    spent = _parse_overrun_warning(
        caplog,
        rule_name="proxy-late-deadline",
        stage="after the RTK probe",
        floor=2.0,
    )
    assert spent > BLOCKING_EFFECT_BUDGET_SECONDS


async def test_spent_budget_warns_once_for_the_whole_event(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A budget spent before the probe is still spent after it.

    Both sites observe the same overrun, so reporting at each would double every
    line in the log for one event.
    """
    del fake_rtk
    _create_rule(manager, "proxy-once", [_proxy_effect()], priority=10)

    with caplog.at_level(logging.WARNING, logger=proxy_hooks.logger.name):
        await RuleEngine(db).evaluate(
            _event(),
            SESSION_ID,
            {},
            blocking_deadline=BlockingEffectDeadline(time.monotonic() - 1.0),
        )

    warnings = [
        record.getMessage() for record in caplog.records if record.name == proxy_hooks.logger.name
    ]
    assert len(warnings) == 1, warnings
    assert "before the RTK probe" in warnings[0]


async def test_rtk_runs_after_same_session_queue_wait(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    del fake_rtk
    _create_rule(manager, "proxy-queued", [_proxy_effect()], priority=10)
    handler = WorkflowHookHandler(rule_engine=RuleEngine(db))
    handler._session_var_manager = MagicMock()
    handler._session_var_manager.get_variables.return_value = {
        "baseline_dirty_files": [],
        "session_edited_files": [],
    }
    event = _event()
    event.metadata["_platform_session_id"] = SESSION_ID
    event.cwd = str(tmp_path)

    held = handler._reserve_eval_lock(SESSION_ID)
    await held.lock.acquire()
    deadline = BlockingEffectDeadline(time.monotonic() - 1.0)
    with monkeypatch.context() as queue_clock:
        queue_clock.setattr(
            "gobby.workflows.hooks.monotonic", MagicMock(side_effect=[100.0, 103.0])
        )
        queued = asyncio.create_task(handler._evaluate_rules(event, blocking_deadline=deadline))
        await drain_asyncio_tasks(cycles=2)
        held.lock.release()
        handler._release_eval_lock(SESSION_ID, held, cleanup=False)

        with caplog.at_level(logging.WARNING, logger=proxy_hooks.logger.name):
            response = await queued

    assert response.modified_input == {"command": "rtk git status"}
    assert not any("blocking deadline exhausted" in message for message in caplog.messages)


async def test_wrong_package_collision_passes_through(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collision = tmp_path / "rtk"
    collision.write_text("#!/bin/sh\necho unrelated\n", encoding="utf-8")
    collision.chmod(0o755)
    monkeypatch.setenv("GOBBY_RTK_BIN", str(collision))
    monkeypatch.setenv("PATH", str(tmp_path))
    _create_rule(manager, "proxy", [_proxy_effect()], priority=10)

    response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert response.modified_input is None


@pytest.mark.parametrize("source", [SessionSource.UNKNOWN, SessionSource.PIPELINE])
async def test_unsupported_provider_skips_rtk(
    source: SessionSource,
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
) -> None:
    _create_rule(manager, "proxy", [_proxy_effect()], priority=10)

    response = await RuleEngine(db).evaluate(
        _event(source=source),
        SESSION_ID,
        {},
    )

    assert response.modified_input is None


async def test_command_is_passed_as_one_argv_without_shell_execution(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "injected"
    argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_RTK_ARGV_LOG", str(argv_log))
    command = f"echo safe; touch {marker}"
    _create_rule(manager, "proxy", [_proxy_effect()], priority=10)

    response = await RuleEngine(db).evaluate(_event(command), SESSION_ID, {})

    assert response.modified_input == {"command": f"rtk {command}"}
    assert not marker.exists()
    invocations = [json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines()]
    assert invocations[-1] == ["rewrite", "--", command]
