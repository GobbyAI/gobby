"""Acceptance coverage for operational configuration consumers."""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, PropertyMock

import pytest

from gobby.cli.tasks import expand as expand_module
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.values import ConfigValuesService
from gobby.hooks import factory as hook_factory
from gobby.mcp_proxy.tools.sessions import _terminal_handoff as terminal_handoff

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPERATIONAL_CONSUMERS = (
    "src/gobby/cli/projects.py",
    "src/gobby/cli/schema.py",
    "src/gobby/cli/sessions.py",
    "src/gobby/cli/tasks/_utils/config.py",
    "src/gobby/cli/tasks/expand.py",
    "src/gobby/hooks/factory.py",
    "src/gobby/mcp_proxy/tools/sessions/_terminal_handoff.py",
)


def test_operational_commands_use_runtime_authority() -> None:
    for relative_path in _OPERATIONAL_CONSUMERS:
        source = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "load_config" not in source, relative_path


def test_command_reads_one_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    config = DaemonConfig()

    class CountingRuntime:
        def __init__(self) -> None:
            self.reads = 0

        def require_config(self) -> DaemonConfig:
            self.reads += 1
            return config

    runtime = CountingRuntime()
    runtime_reads: list[None] = []
    task_manager = object()
    llm_service = object()
    expansion_service = object()
    llm_configs: list[DaemonConfig] = []
    expansion_arguments: list[dict[str, object]] = []

    def get_runtime() -> CountingRuntime:
        runtime_reads.append(None)
        return runtime

    def build_llm(runtime_config: DaemonConfig) -> object:
        llm_configs.append(runtime_config)
        return llm_service

    def build_expansion(**kwargs: object) -> object:
        expansion_arguments.append(kwargs)
        return expansion_service

    monkeypatch.setattr(expand_module, "get_cli_runtime", get_runtime, raising=False)
    monkeypatch.setattr(expand_module, "get_task_manager", lambda: task_manager)
    monkeypatch.setattr(expand_module, "LLMService", build_llm)
    monkeypatch.setattr(expand_module, "ExpansionService", build_expansion)

    result = expand_module._build_expansion_service()

    assert result is expansion_service
    assert len(runtime_reads) == 1
    assert runtime.reads == 1
    assert llm_configs == [config]
    assert expansion_arguments == [
        {"task_manager": task_manager, "llm_service": llm_service, "config": config}
    ]

    second_config = DaemonConfig()

    class AlternatingRuntime:
        def __init__(self) -> None:
            self.reads = 0

        @property
        def snapshot(self) -> SimpleNamespace:
            self.reads += 1
            active = config if self.reads == 1 else second_config
            return SimpleNamespace(active=active)

    machine_runtime = AlternatingRuntime()
    machine_reads: list[None] = []

    def get_config_service() -> SimpleNamespace:
        machine_reads.append(None)
        return SimpleNamespace(runtime=machine_runtime)

    summary_config, compact_config = terminal_handoff._capture_handoff_configs(
        cast(Callable[[], ConfigValuesService], get_config_service),
        session_summary_config=None,
        compact_handoff_config=None,
    )

    assert len(machine_reads) == 1
    assert machine_runtime.reads == 1
    assert summary_config is config.session_summary
    assert compact_config is config.compact_handoff


def test_hook_config_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_config = DaemonConfig(daemon_port=61977)
    runtime = MagicMock()
    snapshot_property = PropertyMock(return_value=SimpleNamespace(active=runtime_config))
    type(runtime).snapshot = snapshot_property
    bootstrap_loader = MagicMock(return_value=BootstrapConfig(daemon_port=61978))
    monkeypatch.setattr(hook_factory, "load_bootstrap", bootstrap_loader, raising=False)

    assert hook_factory.HookManagerFactory._resolve_config(None, runtime) is runtime_config
    snapshot_property.assert_called_once_with()
    bootstrap_loader.assert_not_called()

    bootstrap_config = hook_factory.HookManagerFactory._resolve_config(None, None)

    assert bootstrap_config.daemon_port == 61978
    bootstrap_loader.assert_called_once_with(resolve_database_url=True)
