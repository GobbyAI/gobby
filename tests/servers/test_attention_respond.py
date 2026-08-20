"""Acceptance tests for structured attention responses."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.tmux.text_injection import AttentionInjectionError
from gobby.app_context import ServiceContainer
from gobby.config.bootstrap import BootstrapConfig
from gobby.servers.http import HTTPServer
from gobby.servers.routes.attention import (
    AttentionAnswer,
    AttentionPane,
    create_attention_router,
)
from gobby.storage.attention import AttentionState, AttentionStateManager
from gobby.storage.hub.protocol import HubDatabase
from tests.agents.detection_test_support import BundledDetectionRegistry

DETECTION_REGISTRY = BundledDetectionRegistry()
pytestmark = pytest.mark.unit

APPROVAL_PROMPT = (
    "Tool call needs your approval.\n1. Allow / 2. Cancel\nPress Enter to approve this command\n"
)

AttentionInjector = Callable[[AttentionPane, AttentionAnswer], Awaitable[None]]


def _manager(
    temp_db: HubDatabase,
    *,
    events: list[dict[str, object]] | None = None,
) -> AttentionStateManager:
    return AttentionStateManager(
        temp_db,
        event_publisher=events.append if events is not None else None,
        epoch="attention-test",
    )


def _open_prompt(
    manager: AttentionStateManager,
    *,
    entry_id: str = "run:run-1",
    prompt: str = APPROVAL_PROMPT,
) -> AttentionState:
    detector = PromptDetector(DETECTION_REGISTRY, "claude")
    detected = detector.detect_prompt(prompt)
    assert detected is not None
    result = manager.transition(
        entry_id,
        state="blocked",
        run_id="run-1",
        session_id="session-1",
        reason=detected.kind,
        kind="actionable",
        fingerprint=detected.fingerprint,
        payload=detected.to_payload(),
    )
    assert result.current is not None
    return result.current


def _client(
    manager: AttentionStateManager,
    *,
    capture: Callable[[], Awaitable[str | None]],
    injector: AttentionInjector,
) -> TestClient:
    async def run_db(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    async def resolve_pane(_state: AttentionState) -> AttentionPane:
        return AttentionPane(target="%42", tmux_cmd=("tmux",), capture=capture)

    server = SimpleNamespace(
        services=SimpleNamespace(
            attention_manager=manager,
            agent_lifecycle_monitor=SimpleNamespace(
                prompt_detector=PromptDetector(DETECTION_REGISTRY, "claude")
            ),
            run_db=run_db,
        )
    )
    app = FastAPI()
    app.include_router(
        create_attention_router(
            server,
            pane_resolver=resolve_pane,
            injector=injector,
        )
    )
    return TestClient(app)


def _request(state: AttentionState, answer: dict[str, object]) -> dict[str, object]:
    assert state.fingerprint is not None
    return {
        "attention_id": state.attention_id,
        "fingerprint": state.fingerprint,
        "answer": answer,
    }


def test_respond_cas_and_recurrence(temp_db: HubDatabase) -> None:
    manager = _manager(temp_db)
    state = _open_prompt(manager)
    pane_output = APPROVAL_PROMPT
    injected: list[AttentionAnswer] = []

    async def capture() -> str:
        return pane_output

    async def inject(_pane: AttentionPane, answer: AttentionAnswer) -> None:
        injected.append(answer)

    with _client(manager, capture=capture, injector=inject) as client:
        stale = client.post(
            f"/api/attention/{state.entry_id}/respond",
            json={**_request(state, {"option": 1}), "attention_id": "retired"},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"] == {
            "code": "stale_episode",
            "attention_id": state.attention_id,
            "fingerprint": state.fingerprint,
        }

        invalid_option = client.post(
            f"/api/attention/{state.entry_id}/respond",
            json=_request(state, {"option": 9}),
        )
        assert invalid_option.status_code == 422

        pane_output = APPROVAL_PROMPT.replace("Allow", "Always allow")
        changed = client.post(
            f"/api/attention/{state.entry_id}/respond",
            json=_request(state, {"option": 1}),
        )
        assert changed.status_code == 409
        assert changed.json()["detail"]["code"] == "prompt_changed"
        assert injected == []

        pane_output = APPROVAL_PROMPT
        accepted = client.post(
            f"/api/attention/{state.entry_id}/respond",
            json=_request(state, {"option": 1}),
        )
        assert accepted.status_code == 200
        assert accepted.json() == {"status": "accepted", "entry_id": state.entry_id}
        assert injected[-1].option == 1
        accepted_state = manager.get(state.entry_id)
        assert accepted_state is not None
        assert accepted_state.state is None

        recurring = _open_prompt(manager)
        assert recurring.attention_id != state.attention_id
        text_response = client.post(
            f"/api/attention/{recurring.entry_id}/respond",
            json=_request(recurring, {"text": "approve literally"}),
        )
        assert text_response.status_code == 200
        assert injected[-1].text == "approve literally"

        invalid_variants = client.post(
            f"/api/attention/{recurring.entry_id}/respond",
            json=_request(recurring, {"text": "yes", "key": "enter"}),
        )
        assert invalid_variants.status_code == 422


def test_partial_injection_and_stall_paths(temp_db: HubDatabase) -> None:
    manager = _manager(temp_db)
    detector = PromptDetector(DETECTION_REGISTRY, "claude")
    fingerprint = detector.pane_fingerprint("provider still unavailable")
    stalled = manager.transition(
        "run:run-1",
        state="blocked",
        run_id="run-1",
        session_id="session-1",
        reason="stall",
        kind="non_actionable",
        fingerprint=fingerprint,
        payload=detector.prompt_payload("provider still unavailable", kind="stall").to_payload(),
    ).current
    assert stalled is not None
    failure_stage: str | None = None
    injection_calls = 0

    async def capture() -> str:
        return APPROVAL_PROMPT

    async def inject(_pane: AttentionPane, _answer: AttentionAnswer) -> None:
        nonlocal injection_calls
        injection_calls += 1
        if failure_stage is not None:
            raise AttentionInjectionError(stage=failure_stage)

    with _client(manager, capture=capture, injector=inject) as client:
        rejected = client.post(
            f"/api/attention/{stalled.entry_id}/respond",
            json=_request(stalled, {"key": "escape"}),
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "not_actionable"
        assert injection_calls == 0

        manager.transition(
            stalled.entry_id,
            state=None,
            expected_attention_id=stalled.attention_id,
            expected_fingerprint=stalled.fingerprint,
        )
        actionable = _open_prompt(manager)

        failure_stage = "none"
        failed = client.post(
            f"/api/attention/{actionable.entry_id}/respond",
            json=_request(actionable, {"option": 1}),
        )
        assert failed.status_code == 502
        assert failed.json()["detail"] == {"code": "injection_failed", "stage": "none"}
        retained = manager.get(actionable.entry_id)
        assert retained is not None
        assert retained.attention_id == actionable.attention_id

        failure_stage = "partial"
        partial = client.post(
            f"/api/attention/{actionable.entry_id}/respond",
            json=_request(actionable, {"option": 1}),
        )
        assert partial.status_code == 502
        assert partial.json()["detail"] == {
            "code": "injection_indeterminate",
            "stage": "partial",
        }
        refreshed = manager.get(actionable.entry_id)
        assert refreshed is not None
        assert refreshed.state == "blocked"
        assert refreshed.attention_id != actionable.attention_id


def test_event_driven_option_response(temp_db: HubDatabase) -> None:
    events: list[dict[str, object]] = []
    manager = _manager(temp_db, events=events)
    state = _open_prompt(manager)
    injected: list[AttentionAnswer] = []

    async def capture() -> str:
        return APPROVAL_PROMPT

    async def inject(_pane: AttentionPane, answer: AttentionAnswer) -> None:
        injected.append(answer)

    event = events[-1]
    payload = event["payload"]
    assert isinstance(payload, dict)
    options = payload["options"]
    assert isinstance(options, list)
    selected = options[0]
    assert isinstance(selected, dict)

    with _client(manager, capture=capture, injector=inject) as client:
        response = client.post(
            f"/api/attention/{event['entry_id']}/respond",
            json={
                "attention_id": event["attention_id"],
                "fingerprint": event["fingerprint"],
                "answer": {"option": selected["option"]},
            },
        )

    assert response.status_code == 200
    assert injected[-1].option == selected["option"]
    selected_state = manager.get(state.entry_id)
    assert selected_state is not None
    assert selected_state.state is None


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"option": 1, "text": "yes"},
        {"option": True},
        {"key": "space"},
        {"text": "contains\ttab"},
        {"text": "é" * 1025},
    ],
)
def test_respond_rejects_invalid_answer_contract(
    temp_db: HubDatabase,
    answer: dict[str, object],
) -> None:
    manager = _manager(temp_db)
    state = _open_prompt(manager)
    injected: list[AttentionAnswer] = []

    async def capture() -> str:
        return APPROVAL_PROMPT

    async def inject(_pane: AttentionPane, resolved: AttentionAnswer) -> None:
        injected.append(resolved)

    with _client(manager, capture=capture, injector=inject) as client:
        response = client.post(
            f"/api/attention/{state.entry_id}/respond",
            json=_request(state, answer),
        )

    assert response.status_code == 422
    assert injected == []


def test_respond_returns_404_for_unknown_entry(temp_db: HubDatabase) -> None:
    manager = _manager(temp_db)
    state = _open_prompt(manager)

    async def capture() -> str:
        return APPROVAL_PROMPT

    async def inject(_pane: AttentionPane, _answer: AttentionAnswer) -> None:
        raise AssertionError("unknown entries must not inject")

    with _client(manager, capture=capture, injector=inject) as client:
        response = client.post(
            "/api/attention/run:missing/respond",
            json=_request(state, {"option": 1}),
        )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "attention_not_found"


def test_attention_router_is_registered_in_real_app(temp_db: HubDatabase) -> None:
    services = ServiceContainer(
        database=temp_db,
        session_manager=None,
        task_manager=MagicMock(),
        attention_manager=_manager(temp_db),
        detection_registry=DETECTION_REGISTRY,
    )
    server = HTTPServer(services=services, test_mode=True, bootstrap_config=BootstrapConfig())

    paths = {route.path for route in server.app.routes}

    assert "/api/attention/{entry_id}/respond" in paths


def test_attention_router_composes_session_pane_dependencies(
    temp_db: HubDatabase,
) -> None:
    from gobby.terminals import TerminalRuntimeRegistry
    from tests.terminals.fakes import FakeRuntime, MemoryTerminalStore, make_memory_terminal

    manager = _manager(temp_db)
    state = _open_prompt(manager)
    row = make_memory_terminal()
    row.session_id = "session-1"
    store = MemoryTerminalStore(row)
    runtime = FakeRuntime(snapshot_text=APPROVAL_PROMPT)
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    injected: list[AttentionAnswer] = []

    async def run_db(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    async def inject(_pane: AttentionPane, answer: AttentionAnswer) -> None:
        injected.append(answer)

    server = SimpleNamespace(
        services=SimpleNamespace(
            attention_manager=manager,
            agent_lifecycle_monitor=SimpleNamespace(
                prompt_detector=PromptDetector(DETECTION_REGISTRY, "claude")
            ),
            session_manager=MagicMock(),
            agent_runner=None,
            config=None,
            run_db=run_db,
            terminal_manager=store,
            terminal_runtime_registry=registry,
        )
    )
    app = FastAPI()
    app.include_router(create_attention_router(server, injector=inject))

    with TestClient(app) as client:
        response = client.post(
            f"/api/attention/{state.entry_id}/respond",
            json=_request(state, {"option": 1}),
        )

    assert response.status_code == 200
    assert injected[-1].option == 1


@pytest.mark.parametrize(
    ("answer", "expected_payload", "expected_key"),
    [
        (AttentionAnswer(option=2), "2", "Enter"),
        (AttentionAnswer(text="line one\nline two"), "line one\nline two", "Enter"),
        (AttentionAnswer(key="enter"), None, "Enter"),
        (AttentionAnswer(key="escape"), None, "Escape"),
        (AttentionAnswer(key="tab"), None, "Tab"),
        (AttentionAnswer(key="up"), None, "Up"),
        (AttentionAnswer(key="down"), None, "Down"),
    ],
)
@pytest.mark.asyncio
async def test_attention_injection_sequences(
    answer: AttentionAnswer,
    expected_payload: str | None,
    expected_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.agents.tmux import text_injection

    commands: list[tuple[str, ...]] = []

    async def record(command: Any, *, timeout: float) -> None:
        del timeout
        commands.append(tuple(command))

    monkeypatch.setattr(text_injection, "_run_tmux_command", record)

    await text_injection.inject_attention_answer_to_tmux_target(
        "%42",
        option=answer.option,
        text=answer.text,
        key=answer.key,
        enter_delay_seconds=0,
    )

    send_keys = [command for command in commands if "send-keys" in command]
    assert send_keys[-1][-1] == expected_key
    if expected_payload is None:
        assert len(commands) == 1
    else:
        set_buffer = next(command for command in commands if "set-buffer" in command)
        assert set_buffer[-1] == expected_payload
        assert send_keys == [("tmux", "send-keys", "-t", "%42", "Enter")]


@pytest.mark.parametrize(
    ("fail_on_command", "expected_stage"),
    [(1, "none"), (2, "none"), (4, "partial")],
)
@pytest.mark.asyncio
async def test_attention_injection_failure_stage_tracks_delivered_bytes(
    fail_on_command: int,
    expected_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.agents.tmux import text_injection

    command_count = 0

    async def fail_at_selected_command(command: Any, *, timeout: float) -> None:
        nonlocal command_count
        del command, timeout
        command_count += 1
        if command_count == fail_on_command:
            raise RuntimeError("tmux failed")

    monkeypatch.setattr(text_injection, "_run_tmux_command", fail_at_selected_command)

    with pytest.raises(AttentionInjectionError) as error:
        await text_injection.inject_attention_answer_to_tmux_target(
            "%42",
            option=1,
            enter_delay_seconds=0,
        )

    assert error.value.stage == expected_stage
