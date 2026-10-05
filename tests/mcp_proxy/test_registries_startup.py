"""Registry startup diagnostics must identify an initializer before it returns."""

import importlib
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.storage.hub.protocol import HubDatabase


@pytest.mark.parametrize(
    ("module_name", "attribute", "step"),
    [
        ("gobby.skills.hubs.manager", "resolve_hub_api_keys", "skills hub manager"),
        ("gobby.skills.search", "SkillSearch", "skills search"),
        ("gobby.mcp_proxy.tools.skills", "create_skills_registry", "skills registry"),
        ("gobby.storage.cron", "CronJobStorage", "cron registry"),
        (
            "gobby.mcp_proxy.tools.communications",
            "create_communications_registry",
            "communications registry",
        ),
    ],
)
def test_blocked_registry_startup_reports_active_step(
    hub_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    module_name: str,
    attribute: str,
    step: str,
) -> None:
    module = importlib.import_module(module_name)
    original = getattr(module, attribute)
    entered = Event()
    release = Event()

    def blocked_initializer(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Test did not release the blocked initializer")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, attribute, blocked_initializer)
    caplog.set_level(logging.INFO, logger="gobby.mcp.registries")
    with ThreadPoolExecutor(max_workers=1) as executor:
        startup = executor.submit(
            setup_internal_registries,
            config_resolver=lambda: None,
            db=hub_db,
            communications_manager=MagicMock(),
        )
        try:
            assert entered.wait(5), "Startup never reached the injected initializer"
            assert not startup.done(), "Readiness must still wait for the initializer"
            assert f"Startup step {step} started" in caplog.messages
            assert not any(
                message.startswith(f"Startup step {step} completed") for message in caplog.messages
            )
        finally:
            release.set()
        manager = startup.result(timeout=5)

    assert manager.get_registry("gobby-skills") is not None
    assert any(
        message.startswith(f"Startup step {step} completed in ") for message in caplog.messages
    )
