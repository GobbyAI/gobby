from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.review_requirements import (
    REQUEST_ANCHOR_VARIABLE,
    assemble_requirements_bundle,
    build_request_anchor,
)
from gobby.workflows.observer_plan_mode import resolve_plan_mode

SESSION_ID = "00000000-0000-0000-0000-000000000001"
OBSERVER_PATH = Path(__file__).resolve().parents[2] / "src/gobby/workflows/observer_plan_mode.py"


class _SessionManager:
    def __init__(self, session: Any | None) -> None:
        self.session = session

    def get(self, _session_id: str) -> Any | None:
        return self.session


def _event(
    source: SessionSource,
    *,
    data: dict[str, object],
    metadata: dict[str, object] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=SESSION_ID,
        source=source,
        timestamp=datetime(2026, 7, 28, tzinfo=UTC),
        data=data,
        metadata=metadata or {},
    )


def test_entry_observer_accumulates_request_anchor_into_taskless_bundle(
    tmp_path: Path,
) -> None:
    variables: dict[str, Any] = {
        "chat_mode": "normal",
        "mode_level": 1,
        "plan_mode": False,
        REQUEST_ANCHOR_VARIABLE: build_request_anchor(
            "skill-authored",
            "Wrong request",
        ),
    }
    event = _event(
        SessionSource.CLAUDE,
        data={
            "chat_mode": "plan",
            "request_id": "observed-request",
            "prompt": "Observed initiating request",
        },
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(None))

    anchor = variables[REQUEST_ANCHOR_VARIABLE]
    assert anchor["anchor_id"] == "observed-request"
    assert anchor["content"] == ["Observed initiating request"]
    assert anchor["captured_by"] == "plan_mode_observer"

    later = _event(
        SessionSource.CLAUDE,
        data={
            "chat_mode": "plan",
            "request_id": "later-request",
            "prompt": "Later request",
        },
    )
    resolve_plan_mode(later, variables, SESSION_ID, _SessionManager(None))
    appended = cast(dict[str, object], variables[REQUEST_ANCHOR_VARIABLE])
    assert anchor["content"] == ["Observed initiating request"]
    assert appended["content"] == [
        "Observed initiating request",
        "Later request",
    ]

    first_bundle = assemble_requirements_bundle(
        project_root=tmp_path,
        plan_snapshot=b"# Plan\n",
        request_anchor=appended,
    )
    second_bundle = assemble_requirements_bundle(
        project_root=tmp_path,
        plan_snapshot=b"# Plan\n",
        request_anchor=appended,
    )
    assert first_bundle == second_bundle
    sources = cast(list[dict[str, object]], first_bundle["sources"])
    assert json.loads(cast(str, sources[0]["content"])) == appended["content"]


@pytest.mark.parametrize(
    ("branch", "source"),
    [
        ("web_chat", SessionSource.CLAUDE),
        ("structured", SessionSource.CLAUDE),
        ("codex", SessionSource.CODEX),
        ("provider_native", SessionSource.CLAUDE),
        ("workflow", SessionSource.CLAUDE),
        ("marker", SessionSource.DROID),
    ],
)
def test_anchor_written_on_every_entry_branch(
    branch: str,
    source: SessionSource,
    tmp_path: Path,
) -> None:
    prompt = f"Initiating request for {branch}"
    data: dict[str, object] = {
        "prompt": prompt,
        "request_id": f"request-{branch}",
    }
    metadata: dict[str, object] = {"session_type": "terminal"}
    variables: dict[str, Any] = {
        "chat_mode": "normal",
        "mode_level": 1,
        "plan_mode": False,
    }
    session: Any | None = None

    if branch == "web_chat":
        metadata = {"session_type": "web_chat", "chat_mode": "plan"}
        session = SimpleNamespace(session_type="web_chat", chat_mode="normal")
    elif branch == "structured":
        data["chat_mode"] = "plan"
    elif branch == "codex":
        transcript = tmp_path / "codex.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "turn_context",
                    "payload": {"collaboration_mode": {"mode": "plan"}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        session = SimpleNamespace(session_type="terminal", transcript_path=str(transcript))
    elif branch == "provider_native":
        data["permission_mode"] = "plan"
    elif branch == "workflow":
        variables["chat_mode"] = "plan"
    else:
        data["prompt"] = f"<plan-mode>{prompt}</plan-mode>"
        prompt = cast(str, data["prompt"])

    resolve_plan_mode(
        _event(source, data=data, metadata=metadata),
        variables,
        SESSION_ID,
        _SessionManager(session),
    )

    assert variables["plan_mode"] is True
    anchor = variables[REQUEST_ANCHOR_VARIABLE]
    assert anchor["anchor_id"] == f"request-{branch}"
    assert anchor["content"] == [prompt]


def test_existing_request_anchor_is_reused() -> None:
    persisted = build_request_anchor("request-persisted", "Persisted request")
    reused_variables: dict[str, Any] = {
        "chat_mode": "normal",
        "mode_level": 1,
        "plan_mode": False,
        REQUEST_ANCHOR_VARIABLE: persisted,
    }
    resolve_plan_mode(
        _event(
            SessionSource.CLAUDE,
            data={"chat_mode": "plan"},
        ),
        reused_variables,
        SESSION_ID,
        _SessionManager(None),
    )
    assert reused_variables[REQUEST_ANCHOR_VARIABLE] == persisted


def test_plan_mode_without_request_anchor_raises() -> None:
    with pytest.raises(ReviewEvidenceError) as missing:
        resolve_plan_mode(
            _event(
                SessionSource.CLAUDE,
                data={"chat_mode": "plan"},
            ),
            {"chat_mode": "normal", "mode_level": 1, "plan_mode": False},
            SESSION_ID,
            _SessionManager(None),
        )
    assert missing.value.code == "missing_request_anchor"


def test_single_plan_mode_activation_path() -> None:
    tree = ast.parse(OBSERVER_PATH.read_text(encoding="utf-8"))
    writers: list[str] = []

    class WriterVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function_name = "<module>"

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            prior = self.function_name
            self.function_name = node.name
            self.generic_visit(node)
            self.function_name = prior

        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "plan_mode"
                ):
                    writers.append(self.function_name)
            self.generic_visit(node)

    WriterVisitor().visit(tree)
    assert writers == ["_set_plan_mode_with_anchor"]
