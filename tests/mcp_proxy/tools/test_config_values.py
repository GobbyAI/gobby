from __future__ import annotations

from collections.abc import Collection, Mapping

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.registry import DYNAMIC_SEGMENT_CODEC_VECTORS
from gobby.config.runtime import ApplyFailure, ConfigSnapshot
from gobby.config.values import ConfigValuesService
from gobby.mcp_proxy.tools.config import create_config_registry
from gobby.storage.config_mutations import (
    ConfigConflictError,
    ConfigMutationResult,
    ConfigPatch,
    ConfigRevisionExhaustedError,
)
from gobby.storage.config_repository import MAX_CONFIG_REVISION


class _RecordingConfigService(ConfigValuesService):
    def __init__(self) -> None:
        self.patch_calls: list[tuple[int, Mapping[str, object], Collection[str]]] = []

    async def schema(self) -> dict[str, object]:
        return {"properties": {"websocket.ping_interval": {"type": "number"}}}

    async def values(self) -> dict[str, object]:
        return {"revision": 4, "desired": {}, "active": {}}

    async def patch(
        self,
        *,
        expected_revision: int,
        values: Mapping[str, object],
        unset: Collection[str] = (),
    ) -> dict[str, object]:
        self.patch_calls.append((expected_revision, values, unset))
        return {"committed": True, "revision": 5}


def _snapshot(
    revision: int,
    *,
    desired_values: Mapping[str, object] | None = None,
    active_values: Mapping[str, object] | None = None,
    failed_live_keys: Mapping[str, ApplyFailure] | None = None,
) -> ConfigSnapshot:
    desired = dict(desired_values or {})
    active = dict(active_values if active_values is not None else desired)
    return ConfigSnapshot(
        revision=revision,
        desired=DaemonConfig(),
        active=DaemonConfig(),
        row_revisions=dict.fromkeys(desired, revision),
        pending_restart_keys=frozenset(),
        failed_live_keys=failed_live_keys or {},
        desired_values=desired,
        active_values=active,
        desired_bindings={},
        active_bindings={},
    )


class _FakeRuntime:
    def __init__(self, snapshot: ConfigSnapshot) -> None:
        self.current = snapshot
        self.reconciled = snapshot

    @property
    def snapshot(self) -> ConfigSnapshot:
        return self.current

    async def reconcile_local_commit(self, _revision: int) -> ConfigSnapshot:
        self.current = self.reconciled
        return self.current


class _FakeMutations:
    def __init__(
        self,
        result: ConfigMutationResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or ConfigMutationResult(0, frozenset())
        self.error = error
        self.calls: list[tuple[int, ConfigPatch]] = []

    def patch(self, *, expected_revision: int, patch: ConfigPatch) -> ConfigMutationResult:
        self.calls.append((expected_revision, patch))
        if self.error is not None:
            raise self.error
        return self.result


def _service(
    snapshot: ConfigSnapshot,
    *,
    result: ConfigMutationResult | None = None,
    error: Exception | None = None,
) -> tuple[ConfigValuesService, _FakeRuntime, _FakeMutations]:
    runtime = _FakeRuntime(snapshot)
    mutations = _FakeMutations(result, error)
    return ConfigValuesService(runtime=runtime, mutations=mutations), runtime, mutations


@pytest.mark.asyncio
async def test_mcp_wraps_universal_config_service() -> None:
    service = _RecordingConfigService()
    registry = create_config_registry(lambda: service)

    schema = await registry.call("get_config_schema", {})
    values = await registry.call("get_config_values", {})
    patched = await registry.call(
        "patch_config_values",
        {
            "expected_revision": 4,
            "values": {"websocket": {"ping_interval": 12.0}},
            "unset": ["websocket.ping_timeout"],
        },
    )

    assert schema == {"properties": {"websocket.ping_interval": {"type": "number"}}}
    assert values == {"revision": 4, "desired": {}, "active": {}}
    assert patched == {"committed": True, "revision": 5}
    assert service.patch_calls == [
        (
            4,
            {"websocket": {"ping_interval": 12.0}},
            ["websocket.ping_timeout"],
        )
    ]


@pytest.mark.asyncio
async def test_mcp_patch_requires_revision() -> None:
    service, _runtime, mutations = _service(
        _snapshot(3),
        result=ConfigMutationResult(4, frozenset()),
    )
    registry = create_config_registry(lambda: service)

    schema = registry.get_schema("patch_config_values")
    assert schema is not None
    assert schema["inputSchema"]["required"] == ["expected_revision"]
    with pytest.raises(TypeError):
        await registry.call("patch_config_values", {"values": {}})

    managed = await registry.call(
        "patch_config_values",
        {
            "expected_revision": 3,
            "values": {"ai": {"embeddings": {"model": "replacement"}}},
        },
    )
    secret = await registry.call(
        "patch_config_values",
        {
            "expected_revision": 3,
            "values": {
                "ai": {
                    "generation": {"endpoints": {"openrouter": {"api_key": "classified-secret"}}}
                }
            },
        },
    )

    assert managed["error"]["code"] == "managed_activation_required"
    assert secret == {
        "committed": True,
        "revision": 4,
        "changed_keys": [],
        "apply_status": "applied",
        "pending_restart_keys": [],
        "failed_live_keys": {},
    }
    secret_patch = mutations.calls[-1][1]
    assert secret_patch.values == {}
    assert secret_patch.secrets["ai.generation.endpoints.openrouter.api_key"].plaintext == (
        "classified-secret"
    )

    conflict_service, _runtime, _mutations = _service(
        _snapshot(4),
        error=ConfigConflictError(3, 4),
    )
    conflict = await create_config_registry(lambda: conflict_service).call(
        "patch_config_values",
        {"expected_revision": 3, "values": {}},
    )
    assert conflict == {
        "error": {
            "code": "revision_conflict",
            "message": "Configuration revision is stale",
            "path": ["expected_revision"],
            "retryable": True,
            "expected_revision": 3,
            "actual_revision": 4,
        }
    }


@pytest.mark.asyncio
async def test_mcp_patch_reports_indeterminate_persistence() -> None:
    service, _runtime, _mutations = _service(
        _snapshot(3),
        error=RuntimeError("private database detail"),
    )
    registry = create_config_registry(lambda: service)

    result = await registry.call(
        "patch_config_values",
        {"expected_revision": 3, "values": {"websocket": {"ping_interval": 2.0}}},
    )

    assert result == {
        "error": {
            "code": "persistence_indeterminate",
            "message": "Configuration persistence outcome is indeterminate",
            "path": [],
            "retryable": False,
        }
    }
    assert "private database detail" not in str(result)


def test_legacy_config_tools_are_removed() -> None:
    service = _RecordingConfigService()
    registry = create_config_registry(lambda: service)

    assert [tool["name"] for tool in registry.list_tools()] == [
        "get_config_schema",
        "get_config_values",
        "patch_config_values",
    ]
    for legacy_name in (
        "get_config",
        "get_config_section",
        "set_config",
        "set_config_batch",
        "delete_config",
        "list_config_keys",
        "ensure_defaults",
    ):
        assert legacy_name not in registry


@pytest.mark.asyncio
async def test_mcp_patch_reports_apply_status() -> None:
    key = "websocket.ping_interval"
    service, runtime, _mutations = _service(
        _snapshot(8),
        result=ConfigMutationResult(9, frozenset({key})),
    )
    runtime.reconciled = _snapshot(
        9,
        desired_values={key: 22.0},
        active_values={key: 10.0},
        failed_live_keys={key: ApplyFailure(9, "websocket", frozenset({key}), "private-detail")},
    )
    registry = create_config_registry(lambda: service)

    result = await registry.call(
        "patch_config_values",
        {
            "expected_revision": 8,
            "values": {"websocket": {"ping_interval": 22.0}},
        },
    )

    assert result == {
        "committed": True,
        "revision": 9,
        "changed_keys": [key],
        "apply_status": "failed_live",
        "pending_restart_keys": [],
        "failed_live_keys": {key: {"revision": 9, "subscriber": "websocket"}},
    }
    assert "private-detail" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(("logical", "encoded"), DYNAMIC_SEGMENT_CODEC_VECTORS)
async def test_mcp_round_trips_codec_vectors(logical: str, encoded: str) -> None:
    key = f"ai.generation.endpoints.{encoded}.model"
    service, runtime, mutations = _service(
        _snapshot(1),
        result=ConfigMutationResult(2, frozenset({key})),
    )
    runtime.reconciled = _snapshot(2, desired_values={key: logical})
    registry = create_config_registry(lambda: service)

    patched = await registry.call(
        "patch_config_values",
        {
            "expected_revision": 1,
            "values": {"ai": {"generation": {"endpoints": {encoded: {"model": logical}}}}},
        },
    )
    values = await registry.call("get_config_values", {})

    assert patched["revision"] == 2
    assert mutations.calls[-1][1].values == {key: logical}
    assert values["desired"]["ai"]["generation"]["endpoints"][encoded]["model"] == logical


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [True, -1, MAX_CONFIG_REVISION + 1, 1.0, "1"])
async def test_mcp_revision_domain_and_exhaustion(revision: object) -> None:
    service, _runtime, mutations = _service(_snapshot(0))
    registry = create_config_registry(lambda: service)

    invalid = await registry.call(
        "patch_config_values",
        {
            "expected_revision": revision,
            "values": {"websocket": {"ping_interval": 2.0}},
        },
    )

    assert invalid["error"]["code"] == "validation_error"
    assert invalid["error"]["path"] == ["expected_revision"]
    assert invalid["error"]["retryable"] is False
    assert mutations.calls == []

    exhausted_service, _runtime, _mutations = _service(
        _snapshot(MAX_CONFIG_REVISION),
        error=ConfigRevisionExhaustedError(),
    )
    exhausted_registry = create_config_registry(lambda: exhausted_service)
    exhausted = await exhausted_registry.call(
        "patch_config_values",
        {
            "expected_revision": MAX_CONFIG_REVISION,
            "values": {"websocket": {"ping_interval": 2.0}},
        },
    )
    assert exhausted == {
        "error": {
            "code": "revision_exhausted",
            "message": "Configuration revision cannot be advanced",
            "path": ["expected_revision"],
            "retryable": False,
        }
    }
