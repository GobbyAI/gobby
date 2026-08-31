from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

import pytest

from gobby.providers.capabilities.collectors import CapabilityCollector, validate_snapshot
from gobby.providers.capabilities.collectors.agy import AgyCollector, AgySourceError
from gobby.providers.capabilities.models import SourceState
from gobby.providers.capabilities.refresh import CapabilityRefreshCoordinator, _default_collectors
from gobby.providers.capabilities.seed import apply_seed
from gobby.providers.capabilities.store import ProviderCapabilityStore
from gobby.providers.version_gate import (
    AGY_REQUIRED_VERSION,
    AgySupportRecord,
    ensure_agy_support,
)
from gobby.servers.provider_model_defaults import AGY_MODELS, GEMINI_FAMILY_MODELS
from gobby.storage.hub.protocol import HubDatabase

_CAPTURES = Path("tests/fixtures/provider_contracts/agy/command-captures.json")
_OBSERVED_AT = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
_SUPPORTED = AgySupportRecord(
    installed_version=AGY_REQUIRED_VERSION,
    required_version=AGY_REQUIRED_VERSION,
    supported=True,
    reason=f"AGY {AGY_REQUIRED_VERSION} meets required version {AGY_REQUIRED_VERSION}.",
    identity=None,
)


class _CaptureModel(TypedDict):
    id: str
    label: str


class _CaptureCommandData(TypedDict):
    models: list[_CaptureModel]


class _CaptureCommand(TypedDict):
    data: _CaptureCommandData


class _CaptureStdout(TypedDict):
    command: _CaptureCommand


class _Capture(TypedDict):
    record: str
    exit_code: int
    stdout: _CaptureStdout
    stderr_tail: str


class _CaptureDocument(TypedDict):
    commands: list[_Capture]


def _capture(record: str) -> _Capture:
    document = cast(
        _CaptureDocument,
        json.loads(_CAPTURES.read_text(encoding="utf-8")),
    )
    return next(capture for capture in document["commands"] if capture["record"] == record)


@pytest.mark.asyncio
async def test_collects_record_1_1_20_model_rows() -> None:
    capture = _capture("1.1.20 models JSON (global flag before subcommand)")
    expected = capture["stdout"]["command"]["data"]["models"]
    commands: list[tuple[str, ...]] = []

    async def run_command(command: tuple[str, ...]) -> tuple[int, str, str]:
        commands.append(command)
        return (
            int(capture["exit_code"]),
            json.dumps(capture["stdout"]),
            str(capture["stderr_tail"]),
        )

    collector = AgyCollector(
        run_command=run_command,
        support_record=lambda: _SUPPORTED,
        clock=lambda: _OBSERVED_AT,
    )

    snapshot = validate_snapshot(await collector.collect(), collector.sources)

    assert commands == [("agy", "--output-format", "json", "models")]
    assert [(model.canonical_model, model.display_name) for model in snapshot.models] == [
        (row["id"], row["label"]) for row in expected
    ]
    assert len(snapshot.models) == 14
    assert {source.source_key for source in snapshot.sources} == {"agy_models_cli"}
    assert all(
        model.provenance["canonical_model"].source_key == "agy_models_cli"
        for model in snapshot.models
    )
    assert all(
        model.provenance["context_length"].source_key == "bundled" for model in snapshot.models
    )
    assert snapshot.models[0].aliases == ("gemini-3.7-flash",)
    assert snapshot.models[0].supported_efforts == ("high",)
    assert snapshot.models[0].routes[0].selector == "gemini-3.7-flash-high"


def test_default_collectors_register_agy() -> None:
    assert isinstance(_default_collectors()["agy"], AgyCollector)


def test_bundled_effort_table_matches_floor_catalog() -> None:
    assert tuple(AGY_MODELS) == (
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.1-pro",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
        "gpt-oss-120b",
    )
    gemini_3_5 = next(
        model for model in GEMINI_FAMILY_MODELS if model["value"] == "gemini-3.5-flash"
    )
    assert AGY_MODELS["gemini-3.5-flash"]["reasoning"]["default_effort"] == "medium"
    assert gemini_3_5["reasoning"]["default_effort"] == "medium"
    assert {model["availability_source"] for model in AGY_MODELS.values()} == {"bundled"}

    retired_label = "agy-1.0.10-" + "static"
    matches = [
        str(path)
        for root in (Path("src/gobby"), Path("tests"))
        for path in root.rglob("*")
        if path.suffix in {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs"}
        and retired_label in path.read_text(encoding="utf-8")
    ]
    assert matches == []


@pytest.mark.asyncio
async def test_sub_floor_support_is_a_typed_source_failure() -> None:
    calls = 0

    async def run_command(command: tuple[str, ...]) -> tuple[int, str, str]:
        nonlocal calls
        calls += 1
        return (0, "{}", "")

    unsupported = AgySupportRecord(
        installed_version="1.1.17",
        required_version=AGY_REQUIRED_VERSION,
        supported=False,
        reason="AGY 1.1.17 is below the supported floor.",
        identity=None,
    )

    with pytest.raises(AgySourceError, match="unsupported_version") as error:
        await AgyCollector(
            run_command=run_command,
            support_record=lambda: unsupported,
        ).collect()

    assert error.value.code == "unsupported_version"
    assert error.value.source_key == "agy_models_cli"
    assert calls == 0


@pytest.mark.asyncio
async def test_absent_binary_is_a_typed_source_failure() -> None:
    async def missing_binary(_command: tuple[str, ...]) -> tuple[int, str, str]:
        raise FileNotFoundError("agy executable is unavailable")

    with pytest.raises(AgySourceError, match="binary_unavailable") as error:
        await AgyCollector(
            run_command=missing_binary,
            support_record=lambda: _SUPPORTED,
        ).collect()

    assert error.value.code == "binary_unavailable"
    assert error.value.source_key == "agy_models_cli"


async def test_generic_nonzero_exit_is_a_typed_source_failure() -> None:
    async def command_failed(_command: tuple[str, ...]) -> tuple[int, str, str]:
        return (2, "", "catalog unavailable")

    with pytest.raises(AgySourceError, match="command_failed") as error:
        await AgyCollector(
            run_command=command_failed,
            support_record=lambda: _SUPPORTED,
        ).collect()

    assert error.value.code == "command_failed"
    assert error.value.source_key == "agy_models_cli"


async def test_unauthenticated_exit_is_a_typed_source_failure() -> None:
    capture = next(
        capture
        for capture in json.loads(_CAPTURES.read_text(encoding="utf-8"))["commands"]
        if str(capture["record"]).startswith(
            "1.1.20 unauthenticated `agy --output-format json models`"
        )
    )

    async def run_command(command: tuple[str, ...]) -> tuple[int, str, str]:
        return (1, "", str(capture["stderr_tail"]))

    with pytest.raises(AgySourceError, match="unauthenticated") as error:
        await AgyCollector(
            run_command=run_command,
            support_record=lambda: _SUPPORTED,
        ).collect()

    assert error.value.code == "unauthenticated"
    assert "Please sign in" in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not-json",
        json.dumps({"status": "SUCCESS", "command": {"name": "models", "data": {}}}),
        json.dumps(
            {
                "status": "SUCCESS",
                "command": {"name": "models", "data": {"models": [{"id": "missing-label"}]}},
            }
        ),
    ],
)
@pytest.mark.asyncio
async def test_shape_mismatch_is_a_typed_source_failure(payload: str) -> None:
    async def run_command(command: tuple[str, ...]) -> tuple[int, str, str]:
        return (0, payload, "")

    with pytest.raises(AgySourceError, match="invalid_payload") as error:
        await AgyCollector(
            run_command=run_command,
            support_record=lambda: _SUPPORTED,
        ).collect()

    assert error.value.code == "invalid_payload"


@pytest.mark.integration
async def test_failed_agy_refresh_retains_seeded_snapshot(postgres_db: HubDatabase) -> None:
    store = ProviderCapabilityStore(postgres_db)
    apply_seed(store)
    seeded = store.get_provider_snapshot("agy")
    assert seeded is not None

    async def unauthenticated(_command: tuple[str, ...]) -> tuple[int, str, str]:
        return (1, "", "Please sign in")

    collector = AgyCollector(
        run_command=unauthenticated,
        support_record=lambda: _SUPPORTED,
    )
    coordinator = CapabilityRefreshCoordinator(
        store,
        {"agy": cast(CapabilityCollector, collector)},
    )

    await coordinator.refresh_all()

    retained = store.get_provider_snapshot("agy")
    assert retained is not None
    assert retained.models == seeded.models
    sources = {source.source_key: source for source in retained.sources}
    assert sources["agy_models_cli"].state is SourceState.STALE
    assert sources["agy_models_cli"].last_error is not None
    assert "unauthenticated" in sources["agy_models_cli"].last_error


@pytest.mark.skipif(
    os.environ.get("GOBBY_RUN_AGY_MODELS_LIVE") != "1",
    reason="set GOBBY_RUN_AGY_MODELS_LIVE=1 to compare the installed AGY catalog",
)
@pytest.mark.integration
@pytest.mark.asyncio
async def test_installed_agy_model_catalog_matches_record_1_1_20() -> None:
    support = await ensure_agy_support()
    expected = _capture("1.1.20 models JSON (global flag before subcommand)")
    expected_rows = expected["stdout"]["command"]["data"]["models"]

    snapshot = await AgyCollector(support_record=lambda: support).collect()

    assert [(model.canonical_model, model.display_name) for model in snapshot.models] == [
        (row["id"], row["label"]) for row in expected_rows
    ]
