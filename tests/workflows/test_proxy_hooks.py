from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.integrations.rtk import clear_probe_cache
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine import proxy_hooks
from gobby.workflows.engine.core import RuleEngine

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
    """Mimic ``rtk rewrite``: 0/3 print the rewrite, 1/2 print nothing."""
    executable = tmp_path / "rtk"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
import time

args = sys.argv[1:]
argv_log = os.environ.get("FAKE_RTK_ARGV_LOG")
if argv_log:
    with open(argv_log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\\n")
if args == ["--version"]:
    print("rtk 0.45.0")
    raise SystemExit(0)
if args == ["rewrite", "--help"]:
    print("Arguments:\\n  [ARGS]...  Raw command to rewrite (e.g. \\"git status\\")")
    raise SystemExit(0)

command = args[args.index("--") + 1] if "--" in args else args[-1]
mode = os.environ.get("FAKE_RTK_MODE", "rewrite")
if mode == "pass":
    raise SystemExit(1)
if mode == "deny":
    raise SystemExit(2)
if mode == "ask":
    sys.stdout.write(f"rtk {{command}}")
    raise SystemExit(3)
if mode == "sleep":
    time.sleep(2)
    raise SystemExit(1)
if mode == "invalid":
    os.write(1, b"\\xff")
    raise SystemExit(0)
if mode == "oversized":
    os.write(1, b"x" * (70 * 1024))
    raise SystemExit(0)
if mode == "unexpected":
    raise SystemExit(7)
sys.stdout.write(f"rtk {{command}}")
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("GOBBY_RTK_BIN", str(executable))
    return executable


def _event(
    command: str = "git status",
    *,
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=source,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Bash", "tool_input": {"command": command}},
    )


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
    definition = document["rtk-command-rewrite"]

    assert definition["enabled"] is False
    assert definition["priority"] == 90
    assert definition["event"] == "before_tool"
    assert definition["tools"] == ["Bash"]
    assert definition["effects"] == [{"type": "proxy_hook", "handler": "rtk", "timeout_seconds": 2}]
    RuleDefinitionBody.model_validate(
        {key: value for key, value in definition.items() if key in {"event", "tools", "effects"}}
    )


async def test_rtk_rewrite_is_permission_neutral(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
) -> None:
    _create_rule(manager, "rtk", [_proxy_effect()], priority=90)

    response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert response.modified_input == {"command": "rtk git status"}
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
        blocking_deadline: float | None,
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


async def test_rtk_ask_verdict_applies_rewrite(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    fake_rtk: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_RTK_MODE", "ask")
    _create_rule(manager, "proxy-ask", [_proxy_effect()], priority=10)

    response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert response.modified_input == {"command": "rtk git status"}
    assert response.permission_decision is None
    assert response.auto_approve is False


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


@pytest.mark.parametrize("mode", ["invalid", "oversized", "unexpected"])
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
) -> None:
    monkeypatch.setenv("FAKE_RTK_MODE", "sleep")
    _create_rule(manager, "proxy-timeout", [_proxy_effect(timeout_seconds=0.1)], priority=10)

    response = await RuleEngine(db).evaluate(_event(), SESSION_ID, {})

    assert response.modified_input is None


async def test_exhausted_shared_deadline_skips_rtk(
    db: HubDatabase,
    manager: RuleDefinitionManager,
) -> None:
    _create_rule(manager, "proxy-deadline", [_proxy_effect()], priority=10)

    response = await RuleEngine(db).evaluate(
        _event(),
        SESSION_ID,
        {},
        blocking_deadline=0.0,
    )

    assert response.modified_input is None


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
