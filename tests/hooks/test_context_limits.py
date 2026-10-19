"""Ship-limit reads from the hooks config section (#23359)."""

from types import SimpleNamespace
from typing import Any

import pytest

from gobby import app_context
from gobby.hooks.context_limits import additional_context_limit_for
from gobby.llm.sdk_utils import ADDITIONAL_CONTEXT_LIMIT

pytestmark = pytest.mark.unit


class _HooksSnapshot:
    """Serves only the hooks section, the one read the hook path is allowed to make."""

    def __init__(self, hooks: Any) -> None:
        self._hooks = hooks

    def active_section(self, name: str) -> Any:
        assert name == "hooks", name
        return self._hooks


def _install_hooks(monkeypatch: pytest.MonkeyPatch, hooks: Any) -> None:
    runtime = SimpleNamespace(snapshot=_HooksSnapshot(hooks))
    container = SimpleNamespace(config_runtime=runtime)
    monkeypatch.setattr(app_context, "get_app_context", lambda: container)


def test_configured_limit_is_read_through_the_hooks_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks = SimpleNamespace(additional_context_limit=4321, additional_context_limits={})
    _install_hooks(monkeypatch, hooks)

    assert additional_context_limit_for("claude") == 4321


def test_per_provider_override_wins_over_configured_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks = SimpleNamespace(
        additional_context_limit=4321,
        additional_context_limits={"claude": 999},
    )
    _install_hooks(monkeypatch, hooks)

    assert additional_context_limit_for("claude") == 999
    assert additional_context_limit_for("codex") == 4321


def test_default_applies_without_an_app_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_context, "get_app_context", lambda: None)

    assert additional_context_limit_for("claude") == ADDITIONAL_CONTEXT_LIMIT
