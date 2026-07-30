from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.hooks.events import HookEvent, HookEventType, SessionSource

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
    assert writers == ["_set_plan_mode"]
