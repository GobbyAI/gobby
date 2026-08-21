"""Per-operation configuration and service resolution coverage."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from gobby.app_context import ServiceContainer
from gobby.config.app import DaemonConfig
from gobby.config.features import ToolResultOffloadConfig
from gobby.config.runtime import RuntimeActiveBundle
from gobby.config.runtime_models import ConfigSnapshot
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.mcp_proxy.services.recommendation import RecommendationService
from gobby.mcp_proxy.services.result_offload import _WRAPPER_MUTATION_RESERVE
from gobby.mcp_proxy.services.server_mgmt import ServerManagementService
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.results import _MAX_SLICE_CHARS, create_results_registry
from gobby.mcp_proxy.tools.sessions._terminal_handoff import _capture_handoff_configs
from gobby.mcp_proxy.tools.skills import hub_tools, search_skills
from gobby.mcp_proxy.tools.skills._context import SkillsContext
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    derive_close_transcript_evidence,
)
from gobby.servers.http import HTTPServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils import tool_summarizer
from gobby.worktrees.merge.resolver import MergeResolver

pytestmark = pytest.mark.unit


class _LiveRuntime:
    def __init__(self, config: DaemonConfig) -> None:
        self.ready = True
        self.set_active(config)

    def set_active(self, config: DaemonConfig) -> None:
        self._bundle = RuntimeActiveBundle(
            snapshot=ConfigSnapshot(
                revision=1,
                desired=config,
                active=config,
                row_revisions={},
                pending_restart_keys=frozenset(),
                failed_live_keys={},
            ),
            services=MappingProxyType({}),
        )

    def capture(self) -> RuntimeActiveBundle:
        return self._bundle


def test_http_config_reuses_one_projection_per_epoch() -> None:
    runtime = _LiveRuntime(DaemonConfig(gobby_tasks={"show_result_on_create": False}))
    services = ServiceContainer(
        database=MagicMock(),
        session_manager=None,
        task_manager=MagicMock(),
        config_runtime=cast(Any, runtime),
    )
    with patch("gobby.servers.app_factory.create_app", return_value=FastAPI()):
        server = HTTPServer(
            services=services,
            startup_config=DaemonConfig(),
            test_mode=True,
        )

    first = server.config
    second = server.config

    assert first is second
    assert first is not None
    assert first.gobby_tasks.show_result_on_create is False

    runtime.set_active(DaemonConfig(gobby_tasks={"show_result_on_create": True}))
    third = server.config

    assert third is not first
    assert third is not None
    assert third.gobby_tasks.show_result_on_create is True


@pytest.mark.asyncio
async def test_merge_resolver_uses_live_config_and_llm_service(tmp_path: Path) -> None:
    conflict_path = tmp_path / "conflict.py"
    conflict_path.write_text(
        "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n",
        encoding="utf-8",
    )
    configs = [DaemonConfig().merge_resolution, DaemonConfig().merge_resolution]
    services = [MagicMock(), MagicMock()]
    services[0].call_feature = AsyncMock(return_value="merged_one")
    services[1].call_feature = AsyncMock(return_value="merged_two")
    active = {"index": 0}
    resolver = MergeResolver(
        config_resolver=lambda: configs[active["index"]],
        llm_service_resolver=lambda: services[active["index"]],
    )
    conflicts = [
        {
            "file": str(conflict_path),
            "hunks": [{"ours": "ours", "theirs": "theirs"}],
        }
    ]

    first = await resolver._resolve_conflicts_only(conflicts)
    active["index"] = 1
    second = await resolver._resolve_conflicts_only(conflicts)

    assert first["success"] is True
    assert second["success"] is True
    assert services[0].call_feature.await_args.args[0] is configs[0]
    assert services[1].call_feature.await_args.args[0] is configs[1]


def test_skills_registry_rebuilds_live_hub_and_search_dependencies() -> None:
    configs = [
        DaemonConfig(
            skills={"hubs": {"first": {"type": "clawdhub"}}},
            embeddings={"model": "embed-one", "dim": 768},
        ),
        DaemonConfig(
            skills={"hubs": {"second": {"type": "clawdhub"}}},
            embeddings={"model": "embed-two", "dim": 1024},
        ),
    ]
    active = {"index": 0}
    hub_managers = [MagicMock(), MagicMock()]
    searches = [MagicMock(), MagicMock()]
    captured: dict[str, Any] = {}
    db = cast(HubDatabase, MagicMock())

    def capture_skills_registry(**kwargs: Any) -> InternalToolRegistry:
        captured.update(kwargs)
        return InternalToolRegistry(name="gobby-skills", description="test")

    with (
        patch("gobby.skills.hubs.HubManager", side_effect=hub_managers) as hub_class,
        patch("gobby.skills.search.SkillSearch", side_effect=searches) as search_class,
        patch("gobby.storage.secrets.SecretStore", return_value=MagicMock()),
        patch(
            "gobby.skills.hubs.manager.resolve_hub_api_keys",
            return_value={},
        ) as resolve_keys,
        patch(
            "gobby.mcp_proxy.tools.skills.create_skills_registry",
            side_effect=capture_skills_registry,
        ),
    ):
        setup_internal_registries(
            config_resolver=lambda: configs[active["index"]],
            db=db,
        )
        resolve_hub = cast(Callable[[], Any], captured["hub_manager_resolver"])
        resolve_search = cast(Callable[[], Any], captured["search_resolver"])

        assert resolve_hub() is hub_managers[0]
        assert resolve_search() is searches[0]

        active["index"] = 1
        assert resolve_hub() is hub_managers[1]
        assert resolve_search() is searches[1]

    assert hub_class.call_args_list[1].kwargs["configs"] is configs[1].skills.hubs
    assert search_class.call_args_list[1].kwargs["config"] is configs[1].search
    assert search_class.call_args_list[1].kwargs["embedding_model"] == "embed-two"
    assert search_class.call_args_list[1].kwargs["embedding_dim"] == 1024
    assert hub_managers[1]._skill_description_config is configs[1].skill_description
    assert resolve_keys.call_args_list[1].args[0] is configs[1].skills.hubs


@pytest.mark.asyncio
async def test_skill_tool_bodies_use_live_resolvers() -> None:
    storage = MagicMock()
    storage.list_skills.return_value = []
    storage.get_skills_by_ids.return_value = []
    notifier = MagicMock()
    hubs = [MagicMock(), MagicMock()]
    searches = [MagicMock(), MagicMock()]
    hubs[0].list_hubs.return_value = []
    hubs[1].list_hubs.return_value = []
    for search in searches:
        search.index_attempted = True
        search.search_async = AsyncMock(return_value=[])
    active = {"index": 0}
    context = SkillsContext(
        db=cast(HubDatabase, MagicMock()),
        storage=storage,
        notifier=notifier,
        session_manager=MagicMock(),
        search=searches[0],
        updater=MagicMock(),
        loader=MagicMock(),
        project_id=None,
        hub_manager=hubs[0],
        search_resolver=lambda: searches[active["index"]],
        hub_manager_resolver=lambda: hubs[active["index"]],
    )
    registry = InternalToolRegistry(name="gobby-skills", description="test")
    hub_tools.register(context, registry)
    search_skills.register(context, registry)

    first_hubs = await registry.call("list_hubs", {})
    first_search = await registry.call("search_skills", {"query": "one"})
    active["index"] = 1
    second_hubs = await registry.call("list_hubs", {})
    second_search = await registry.call("search_skills", {"query": "two"})

    assert first_hubs == {"success": True, "count": 0, "hubs": []}
    assert second_hubs == first_hubs
    assert first_search == {"success": True, "count": 0, "results": []}
    assert second_search == first_search
    hubs[0].list_hubs.assert_called_once_with()
    hubs[1].list_hubs.assert_called_once_with()
    searches[0].search_async.assert_awaited_once()
    searches[1].search_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_summarizer_uses_live_config_and_llm_service() -> None:
    configs = [DaemonConfig().tool_summarizer, DaemonConfig().tool_summarizer]
    services = [MagicMock(), MagicMock()]
    services[0].call_feature = AsyncMock(return_value="summary one")
    services[1].call_feature = AsyncMock(return_value="summary two")
    active = {"index": 0}
    loader = MagicMock()
    loader.render.return_value = "summarize this"
    tool_summarizer.init_summarizer_config(
        lambda: configs[active["index"]],
        db=cast(HubDatabase, MagicMock()),
        llm_service_resolver=lambda: services[active["index"]],
    )

    try:
        with patch.object(tool_summarizer, "_loader", loader):
            first = await tool_summarizer._summarize_description_with_llm("one" * 100)
            active["index"] = 1
            second = await tool_summarizer._summarize_description_with_llm("two" * 100)
    finally:
        tool_summarizer.reset_summarizer_config()

    assert first == "summary one"
    assert second == "summary two"
    assert services[0].call_feature.await_args.args[0] is configs[0]
    assert services[1].call_feature.await_args.args[0] is configs[1]


@pytest.mark.asyncio
async def test_recommendation_and_import_use_live_llm_service() -> None:
    response = '{"recommendations": [{"server": "s", "tool": "t", "reason": "r"}]}'
    llm_services = [MagicMock(), MagicMock()]
    for service in llm_services:
        service.call_feature = AsyncMock(return_value=response)
    configs = [DaemonConfig(), DaemonConfig()]
    active = {"index": 0}
    mcp_manager = MagicMock()
    mcp_manager.get_available_servers.return_value = ["gobby-tasks"]
    recommendation = RecommendationService(
        llm_service=llm_services[0],
        mcp_manager=mcp_manager,
        db=MagicMock(),
        config_resolver=lambda: configs[active["index"]].recommend_tools,
        llm_service_resolver=lambda: llm_services[active["index"]],
    )
    recommendation._loader = MagicMock()
    recommendation._loader.render.return_value = "recommend"

    await recommendation.recommend_tools("first", search_mode="llm")
    active["index"] = 1
    await recommendation.recommend_tools("second", search_mode="llm")

    assert llm_services[0].call_feature.await_count == 1
    assert llm_services[1].call_feature.await_count == 1
    assert llm_services[0].call_feature.await_args.args[0] is configs[0].recommend_tools
    assert llm_services[1].call_feature.await_args.args[0] is configs[1].recommend_tools

    importer = MagicMock()
    importer.import_from_project = AsyncMock(return_value={"success": True})
    management = ServerManagementService(
        mcp_manager,
        config_manager=MagicMock(db=MagicMock()),
        config_resolver=lambda: configs[active["index"]],
        llm_service=llm_services[0],
        llm_service_resolver=lambda: llm_services[active["index"]],
    )
    active["index"] = 0
    with (
        patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "test-project"},
        ),
        patch(
            "gobby.mcp_proxy.importer.MCPServerImporter",
            return_value=importer,
        ) as importer_class,
    ):
        await management.import_server(from_project="first")
        active["index"] = 1
        await management.import_server(from_project="second")

    assert importer_class.call_args_list[0].kwargs["llm_service"] is llm_services[0]
    assert importer_class.call_args_list[1].kwargs["llm_service"] is llm_services[1]


def test_http_wiring_threads_live_resolvers_and_archive_override(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    configs = [
        DaemonConfig(
            gobby_tasks={"show_result_on_create": False},
            session_lifecycle={"transcript_archive_dir": str(archive_dir)},
        ),
        DaemonConfig(gobby_tasks={"show_result_on_create": True}),
    ]
    llm_services = [MagicMock(), MagicMock()]
    active = {"index": 0}
    services = ServiceContainer(
        database=MagicMock(),
        session_manager=MagicMock(),
        task_manager=MagicMock(),
        llm_service=llm_services[0],
        mcp_manager=MagicMock(),
        mcp_db_manager=MagicMock(db=MagicMock()),
    )

    class RuntimeHTTPServer(HTTPServer):
        def resolve_runtime_config(self) -> DaemonConfig:
            return configs[active["index"]]

    with (
        patch("gobby.worktrees.merge.resolver.MergeResolver") as merge_resolver,
        patch("gobby.storage.merge_resolutions.MergeResolutionManager"),
        patch("gobby.storage.inter_session_messages.InterSessionMessageManager"),
        patch("gobby.sessions.transcript_reader.TranscriptReader") as transcript_reader,
        patch("gobby.storage.unmodeled_observations.UnmodeledObservationStore"),
        patch("gobby.servers.http.setup_internal_registries", return_value=[]) as setup,
        patch("gobby.utils.tool_summarizer.init_summarizer_config") as init_summarizer,
        patch("gobby.servers.http.SemanticToolSearch"),
        patch("gobby.servers.http.GobbyDaemonTools") as daemon_tools,
        patch("gobby.servers.http.create_mcp_server"),
        patch("gobby.servers.app_factory.create_app", return_value=FastAPI()),
    ):
        RuntimeHTTPServer(
            services=services,
            startup_config=configs[0],
            test_mode=True,
        )

    registry_config = setup.call_args.kwargs["config_resolver"]
    tools_config = daemon_tools.call_args.kwargs["config_resolver"]
    tools_operation = daemon_tools.call_args.kwargs["operation_context_factory"]
    registry_llm = setup.call_args.kwargs["llm_service_resolver"]
    tools_llm = daemon_tools.call_args.kwargs["llm_service_resolver"]
    merge_config = merge_resolver.call_args.kwargs["config_resolver"]
    merge_llm = merge_resolver.call_args.kwargs["llm_service_resolver"]
    summarizer_config = init_summarizer.call_args.args[0]
    summarizer_llm = init_summarizer.call_args.kwargs["llm_service_resolver"]

    assert callable(registry_config)
    assert callable(tools_config)
    assert callable(tools_operation)
    assert getattr(registry_config, "__self__", None) is None
    assert getattr(tools_config, "__self__", None) is None
    assert getattr(tools_operation, "__self__", None) is None
    with tools_operation():
        assert tools_config().gobby_tasks.show_result_on_create is False
    assert registry_config().gobby_tasks.show_result_on_create is False
    assert tools_config().gobby_tasks.show_result_on_create is False
    assert merge_config() is configs[0].merge_resolution
    assert registry_llm() is llm_services[0]
    assert tools_llm() is llm_services[0]
    assert merge_llm() is llm_services[0]
    assert summarizer_config() is configs[0].tool_summarizer
    assert summarizer_llm() is llm_services[0]
    assert transcript_reader.call_args.kwargs["archive_dir"] == str(archive_dir)

    active["index"] = 1
    services.llm_service = llm_services[1]

    assert registry_config().gobby_tasks.show_result_on_create is True
    assert tools_config().gobby_tasks.show_result_on_create is True
    assert merge_config() is configs[1].merge_resolution
    assert registry_llm() is llm_services[1]
    assert tools_llm() is llm_services[1]
    assert merge_llm() is llm_services[1]
    assert summarizer_config() is configs[1].tool_summarizer
    assert summarizer_llm() is llm_services[1]


@pytest.mark.asyncio
async def test_results_schema_stays_pinned_and_live_bound_error_is_visible() -> None:
    configs = [
        ToolResultOffloadConfig(max_envelope_chars=3_000),
        ToolResultOffloadConfig(max_envelope_chars=2_000),
    ]
    active = {"index": 0}
    store = MagicMock()
    initial_limit = configs[0].max_envelope_chars - _WRAPPER_MUTATION_RESERVE
    live_limit = configs[1].max_envelope_chars - _WRAPPER_MUTATION_RESERVE
    with (
        patch("gobby.mcp_proxy.tools.results.ToolResultStore", return_value=store),
        patch("gobby.mcp_proxy.tools.results.pick_search_backend", return_value=MagicMock()),
    ):
        registry = create_results_registry(
            cast(HubDatabase, MagicMock()),
            lambda: configs[active["index"]],
            default_project_id="11111111-1111-4111-8111-111111111111",
        )

    schema = registry.get_schema("get_tool_result")
    assert schema is not None
    # The advertised maximum is pinned to the static slice ceiling; the live
    # envelope bound is enforced per call below (#20158).
    assert schema["inputSchema"]["properties"]["limit"]["maximum"] == _MAX_SLICE_CHARS

    active["index"] = 1
    result = await registry.call(
        "get_tool_result",
        {
            "result_id": "11111111-1111-4111-8111-111111111111",
            "limit": initial_limit,
        },
    )

    assert result == {
        "success": False,
        "error": f"limit exceeds current live maximum of {live_limit}",
    }
    store.get_slice.assert_not_called()


def test_compact_handoff_uses_supplied_epoch_config() -> None:
    configs = [DaemonConfig(), DaemonConfig()]
    reads: list[int] = []

    def resolve_config() -> DaemonConfig:
        reads.append(0)
        return configs[0]

    summary, compact = _capture_handoff_configs(
        resolve_config,
        session_summary_config=None,
        compact_handoff_config=None,
    )

    assert reads == [0]
    assert summary is configs[0].session_summary
    assert compact is configs[0].compact_handoff


@pytest.mark.asyncio
async def test_task_close_uses_session_lifecycle_archive_override(tmp_path: Path) -> None:
    archive_dir = str(tmp_path / "archive")
    config = DaemonConfig(
        session_lifecycle={"transcript_archive_dir": archive_dir},
    )
    context = MagicMock()
    context.config = config
    context.session_manager.get.return_value = SimpleNamespace(created_at="start")
    evidence = MagicMock()
    merged = MagicMock()

    with (
        patch(
            "gobby.mcp_proxy.tools.tasks._close_evaluation_support.resolve_validation_detection_config"
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._close_evaluation_support.derive_transcript_evidence",
            new=AsyncMock(return_value=evidence),
        ) as derive,
        patch(
            "gobby.mcp_proxy.tools.tasks._close_evaluation_support.merge_transcript_evidence",
            return_value=merged,
        ),
    ):
        result = await derive_close_transcript_evidence(
            context,
            task_id="task",
            owner_session_id="session",
            closing_session_id="session",
            owner_window_start="start",
            task_edited_files=set(),
            repo_path=str(tmp_path),
        )

    assert result is merged
    assert derive.await_args is not None
    assert derive.await_args.kwargs["archive_dir"] == archive_dir
