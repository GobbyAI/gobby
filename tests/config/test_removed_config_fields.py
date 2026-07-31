"""Contract tests for config fields removed before the 0.5.0 release."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from gobby.config.app import DaemonConfig, load_config
from gobby.config.build import BuildConfig, load_build_config
from gobby.config.code_index import CodeIndexConfig
from gobby.config.communications import CommunicationsConfig
from gobby.config.tasks import TaskExpansionConfig
from gobby.storage.config_store import flatten_config

pytestmark = pytest.mark.unit

REMOVED_CODE_INDEX_FIELDS = {
    "auto_index_on_commit",
    "content_extensions",
    "exclude_patterns",
    "languages",
    "max_file_size_bytes",
    "qdrant_collection_prefix",
}
REMOVED_RESEARCH_FIELDS = {
    "codebase_research_enabled",
    "research_max_steps",
    "research_model",
    "research_system_prompt",
    "research_timeout",
    "web_research_enabled",
}
REMOVED_ACCESSORS = {
    "get_mcp_client_proxy_config",
    "get_memory_backup_config",
    "get_memory_config",
    "get_metrics_config",
    "get_project_verification_synthesis_config",
    "get_recommend_tools_config",
    "get_skills_config",
    "get_tool_summarizer_config",
}
REMOVED_CONFIG_STORE_ROWS = {
    "code_index.auto_index_on_commit": False,
    "code_index.content_extensions": [".txt"],
    "code_index.exclude_patterns": ["vendor"],
    "code_index.languages": ["python"],
    "code_index.max_file_size_bytes": 10,
    "code_index.qdrant_collection_prefix": "code_symbols_",
    "communications.inbound_enabled": False,
    "communications.outbound_enabled": False,
    "context_injection.enabled": False,
    "gobby-tasks.expansion.codebase_research_enabled": False,
    "gobby-tasks.expansion.research_max_steps": 3,
    "gobby-tasks.expansion.research_model": "claude/haiku",
    "gobby-tasks.expansion.research_system_prompt": "Research.",
    "gobby-tasks.expansion.research_timeout": 30,
    "gobby-tasks.expansion.web_research_enabled": False,
    "gobby_tasks.expansion.codebase_research_enabled": False,
    "gobby_tasks.expansion.research_max_steps": 3,
    "gobby_tasks.expansion.research_model": "claude/haiku",
    "gobby_tasks.expansion.research_system_prompt": "Research.",
    "gobby_tasks.expansion.research_timeout": 30,
    "gobby_tasks.expansion.web_research_enabled": False,
}


@pytest.mark.parametrize(
    ("model", "removed_fields"),
    [
        pytest.param(CodeIndexConfig, REMOVED_CODE_INDEX_FIELDS, id="code-index"),
        pytest.param(CommunicationsConfig, {"inbound_enabled", "outbound_enabled"}, id="comms"),
        pytest.param(TaskExpansionConfig, REMOVED_RESEARCH_FIELDS, id="task-expansion"),
    ],
)
def test_removed_fields_are_absent_from_config_models(
    model: type[BaseModel],
    removed_fields: set[str],
) -> None:
    assert removed_fields.isdisjoint(model.model_fields)


def test_context_injection_and_dead_accessors_are_absent_from_daemon_config() -> None:
    assert "context_injection" not in DaemonConfig.model_fields
    assert all(not hasattr(DaemonConfig, accessor) for accessor in REMOVED_ACCESSORS)


def test_build_config_keeps_only_the_consumed_agent_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    project_root = tmp_path / "project"
    (home / ".gobby").mkdir(parents=True)
    (project_root / ".gobby").mkdir(parents=True)
    (home / ".gobby" / "build.yaml").write_text(
        yaml.safe_dump(
            {
                "cleanup_clones_on_merge": False,
                "clones_dir": "/tmp/clones",
                "default_isolation": "clone",
                "default_skip_stages": ["research"],
                "default_target_branch": "main",
                "dispatch_interval_seconds": 15,
                "max_active_agents": 4,
                "stage_caps": "removed",
            }
        )
    )
    monkeypatch.setattr("gobby.config.build.Path.home", lambda: home)

    config = load_build_config(project_root)

    assert {field.name for field in fields(BuildConfig)} == {"max_active_agents"}
    assert config.max_active_agents == 4


def test_code_index_drops_removed_keys_during_final_migration() -> None:
    config = CodeIndexConfig.model_validate(
        {
            "auto_index_on_commit": False,
            "content_extensions": [".txt"],
            "exclude_patterns": ["vendor"],
            "languages": ["python"],
            "max_file_size_bytes": 10,
            "qdrant_collection_prefix": "custom_",
        }
    )

    assert REMOVED_CODE_INDEX_FIELDS.isdisjoint(config.model_dump())


def test_flattened_defaults_emit_no_removed_config_paths() -> None:
    flattened = flatten_config(DaemonConfig().model_dump(mode="json", by_alias=True))

    assert all(not key.startswith("context_injection.") for key in flattened)
    assert REMOVED_CODE_INDEX_FIELDS.isdisjoint(
        key.removeprefix("code_index.") for key in flattened if key.startswith("code_index.")
    )
    assert {
        f"gobby-tasks.expansion.{field_name}" for field_name in REMOVED_RESEARCH_FIELDS
    }.isdisjoint(flattened)
    assert {
        "communications.inbound_enabled",
        "communications.outbound_enabled",
    }.isdisjoint(flattened)


def test_load_config_deletes_rows_for_removed_config_fields(tmp_path: Path) -> None:
    class DummyConfigStore:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def get_all(self) -> dict[str, object]:
            return dict(REMOVED_CONFIG_STORE_ROWS)

        def delete(self, key: str) -> bool:
            self.deleted.append(key)
            return True

    store = DummyConfigStore()

    load_config(
        config_file=str(tmp_path / "bootstrap.yaml"),
        config_store=store,
    )

    assert set(store.deleted) == set(REMOVED_CONFIG_STORE_ROWS)
