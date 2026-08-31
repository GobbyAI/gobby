"""Tests for proxy-side output repair of OpenAPI-backed tool results."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult, TextContent

from gobby.mcp_proxy.models import MCPServerConfig
from gobby.mcp_proxy.services import output_repair
from gobby.mcp_proxy.services.output_repair import (
    DEVIATIONS_KEY,
    DEVIATIONS_TRUNCATED_KEY,
    MAX_DEVIATIONS,
    build_schema_index,
    clear_schema_index_cache,
    get_schema_index,
    maybe_repair_output,
    repair_call_result,
    repair_value,
)
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService

pytestmark = pytest.mark.unit

_PROJECT_ID = "test-project"
_LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
_LONG_OPERATION_ID = "A" * 70

_SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "paths": {
        "/sales": {
            "get": {
                "operationId": "ListSales__get",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SalesPage"}
                            }
                        }
                    }
                },
            }
        },
        "/levels": {
            "get": {
                "summary": "Fetch Item-Levels",
                "responses": {
                    "default": {"description": "error"},
                    "201": {
                        "content": {
                            "text/plain": {"schema": {"type": "string"}},
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/Level"},
                                }
                            },
                        }
                    },
                },
            }
        },
        "/dup": {
            "get": {
                "operationId": "Same",
                "responses": {
                    "200": {"content": {"application/json": {"schema": {"type": "object"}}}}
                },
            },
            "post": {
                "operationId": "Same",
                "responses": {
                    "204": {"content": {"application/json": {"schema": {"type": "boolean"}}}}
                },
            },
        },
        "/ref-response": {
            "get": {
                "operationId": "RefResp",
                "responses": {"200": {"$ref": "#/components/responses/Ok"}},
            }
        },
        "/nothing": {
            "get": {"operationId": "NoSchema", "responses": {"200": {"description": "ok"}}}
        },
        "/long": {
            "get": {
                "operationId": _LONG_OPERATION_ID,
                "responses": {
                    "200": {"content": {"application/json": {"schema": {"type": "integer"}}}}
                },
            }
        },
    },
    "components": {
        "schemas": {
            "SalesPage": {
                "type": "object",
                "properties": {
                    "data": {"type": "array", "items": {"$ref": "#/components/schemas/Sale"}},
                    "version": {
                        "type": "object",
                        "properties": {"min": {"type": "integer"}, "max": {"type": "integer"}},
                    },
                },
            },
            "Sale": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "total": {"type": "number"},
                    "note": {"type": "string", "nullable": True},
                    "customer_id": {"type": ["string", "null"]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "paid": {"type": "boolean"},
                    "meta": {"anyOf": [{"type": "object"}, {"type": "null"}]},
                    "count": {"type": "integer"},
                },
            },
            "Level": {"type": "object", "properties": {"qty": {"type": "number"}}},
        },
        "responses": {
            "Ok": {"content": {"application/json": {"schema": {"type": "integer"}}}},
        },
    },
}


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    clear_schema_index_cache()
    yield
    clear_schema_index_cache()


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", _LOCAL_MACHINE_ID):
        yield


def _sales_schema() -> dict[str, Any]:
    return {"$ref": "#/components/schemas/SalesPage"}


def _repair(value: Any, *, null_policy: str = "drop") -> tuple[Any, list[dict[str, str]]]:
    return repair_value(value, _sales_schema(), document=_SPEC, null_policy=null_policy)


def _write_spec(tmp_path: Path, name: str = "spec.json") -> Path:
    spec_file = tmp_path / name
    if name.endswith(".json"):
        spec_file.write_text(json.dumps(_SPEC), encoding="utf-8")
    else:
        import yaml

        spec_file.write_text(yaml.safe_dump(_SPEC), encoding="utf-8")
    return spec_file


def _openapi_config(spec_file: Path, **values: str) -> MCPServerConfig:
    template_values: dict[str, Any] = {"spec_path": str(spec_file), **values}
    return MCPServerConfig(
        name="lightspeed",
        project_id=_PROJECT_ID,
        url="https://example.test",
        id="lightspeed-id",
        template="openapi",
        template_values=template_values,
        tools=[{"name": "ListSales", "description": "List sales"}],
    )


def _manager_with(config: MCPServerConfig) -> MagicMock:
    manager = MagicMock()
    manager.project_id = _PROJECT_ID
    manager.server_configs = [config]
    manager._configs = {config.id: config}
    manager.get_server_config.side_effect = lambda sid: manager._configs.get(sid)
    manager.has_server.side_effect = lambda sid: sid in manager._configs
    return manager


def _service_with(config: MCPServerConfig) -> SimpleNamespace:
    return SimpleNamespace(_mcp_manager=_manager_with(config))


def _sales_result(
    payload: dict[str, Any], *, structured: bool = True, is_error: bool = False
) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structuredContent=copy.deepcopy(payload) if structured else None,
        isError=is_error,
    )


# ---------------------------------------------------------------------------
# Spec index
# ---------------------------------------------------------------------------


def test_index_names_tools_like_fastmcp_and_picks_json_success_schema() -> None:
    index = build_schema_index(_SPEC)

    assert set(index.tools) == {
        "ListSales",
        "Fetch_Item_Levels",
        "Same",
        "Same_2",
        "RefResp",
        "A" * 56,
    }
    assert index.tools["ListSales"].wrapped is False
    assert index.tools["ListSales"].schema == {"$ref": "#/components/schemas/SalesPage"}
    levels = index.tools["Fetch_Item_Levels"]
    assert levels.wrapped is True
    assert levels.schema["items"] == {"$ref": "#/components/schemas/Level"}
    assert index.tools["Same"].wrapped is False
    assert index.tools["Same_2"].schema == {"type": "boolean"}
    assert index.tools["RefResp"].schema == {"type": "integer"}
    assert index.tools["RefResp"].wrapped is True


# ---------------------------------------------------------------------------
# Repair semantics
# ---------------------------------------------------------------------------


def test_drop_policy_removes_nulls_and_coerces_parseable_scalars() -> None:
    payload = {
        "data": [
            {
                "id": "1",
                "total": "12.50",
                "note": None,
                "customer_id": None,
                "tags": ["a", None],
                "paid": "true",
                "meta": None,
                "count": 3.0,
            }
        ],
        "version": {"min": None, "max": "4"},
        "extra": None,
    }

    repaired, deviations = _repair(payload)

    assert repaired == {
        "data": [
            {
                "id": "1",
                "total": 12.5,
                "note": None,
                "customer_id": None,
                "tags": ["a"],
                "paid": True,
                "meta": None,
                "count": 3,
            }
        ],
        "version": {"max": 4},
        "extra": None,
    }
    assert deviations == [
        {"path": "$.data[0].total", "expected": "number", "actual": "string", "action": "coerced"},
        {"path": "$.data[0].tags[1]", "expected": "string", "actual": "null", "action": "dropped"},
        {"path": "$.data[0].paid", "expected": "boolean", "actual": "string", "action": "coerced"},
        {"path": "$.data[0].count", "expected": "integer", "actual": "number", "action": "coerced"},
        {"path": "$.version.min", "expected": "integer", "actual": "null", "action": "dropped"},
        {"path": "$.version.max", "expected": "integer", "actual": "string", "action": "coerced"},
    ]


def test_empty_policy_substitutes_typed_empty_values() -> None:
    payload = {
        "data": [{"id": None, "tags": None, "total": None, "paid": None, "count": None}],
        "version": None,
    }

    repaired, deviations = _repair(payload, null_policy="empty")

    assert repaired == {
        "data": [{"id": "", "tags": [], "total": 0, "paid": False, "count": 0}],
        "version": {},
    }
    assert {item["action"] for item in deviations} == {"replaced_with_empty"}
    assert [item["path"] for item in deviations] == [
        "$.data[0].id",
        "$.data[0].tags",
        "$.data[0].total",
        "$.data[0].paid",
        "$.data[0].count",
        "$.version",
    ]


def test_unfixable_mismatches_are_reported_unchanged() -> None:
    payload = {"data": [{"total": "abc", "id": {"x": 1}, "paid": "maybe"}]}

    repaired, deviations = _repair(payload)

    assert repaired == payload
    assert deviations == [
        {
            "path": "$.data[0].total",
            "expected": "number",
            "actual": "string",
            "action": "unchanged",
        },
        {"path": "$.data[0].id", "expected": "string", "actual": "object", "action": "unchanged"},
        {
            "path": "$.data[0].paid",
            "expected": "boolean",
            "actual": "string",
            "action": "unchanged",
        },
    ]


def test_double_encoded_json_is_parsed_then_repaired() -> None:
    payload = {"data": json.dumps([{"id": 9, "count": None}])}

    repaired, deviations = _repair(payload)

    assert repaired == {"data": [{"id": "9"}]}
    assert [item["action"] for item in deviations] == ["parsed_json", "coerced", "dropped"]


def test_conforming_payload_reports_no_deviations() -> None:
    payload = {"data": [{"id": "1", "total": 3, "note": None, "meta": {"k": "v"}}]}

    repaired, deviations = _repair(payload)

    assert repaired == payload
    assert deviations == []


def test_root_null_under_drop_policy_is_kept_and_reported() -> None:
    repaired, deviations = _repair(None)

    assert repaired is None
    assert deviations == [
        {"path": "$", "expected": "object", "actual": "null", "action": "dropped"}
    ]


# ---------------------------------------------------------------------------
# CallToolResult repair
# ---------------------------------------------------------------------------


def test_repair_call_result_rewrites_text_and_structured_and_attaches_deviations() -> None:
    index = build_schema_index(_SPEC)
    result = _sales_result({"data": [{"id": "1", "count": "2", "tags": [None]}]})

    repaired, deviations = repair_call_result(
        result, index.tools["ListSales"], index, null_policy="drop"
    )

    expected_payload = {"data": [{"id": "1", "count": 2, "tags": []}]}
    assert repaired.structured_content == {**expected_payload, DEVIATIONS_KEY: deviations}
    assert [item["path"] for item in deviations] == ["$.data[0].count", "$.data[0].tags[0]"]
    assert isinstance(repaired.content[0], TextContent)
    assert json.loads(repaired.content[0].text) == expected_payload
    assert len(repaired.content) == 1


def test_repair_call_result_returns_same_object_without_deviations() -> None:
    index = build_schema_index(_SPEC)
    result = _sales_result({"data": [{"id": "1"}]})

    repaired, deviations = repair_call_result(
        result, index.tools["ListSales"], index, null_policy="drop"
    )

    assert repaired is result
    assert deviations == []


def test_repair_call_result_handles_wrapped_scalar_payload() -> None:
    index = build_schema_index(_SPEC)
    result = CallToolResult(
        content=[TextContent(type="text", text='"7"')],
        structuredContent={"result": "7"},
    )

    repaired, deviations = repair_call_result(
        result, index.tools["RefResp"], index, null_policy="drop"
    )

    assert repaired.structured_content == {"result": 7, DEVIATIONS_KEY: deviations}
    assert deviations == [
        {"path": "$", "expected": "integer", "actual": "string", "action": "coerced"}
    ]
    assert isinstance(repaired.content[0], TextContent)
    assert repaired.content[0].text == "7"


def test_repair_call_result_appends_report_when_no_structured_content() -> None:
    index = build_schema_index(_SPEC)
    result = _sales_result({"data": [{"id": 5}]}, structured=False)

    repaired, deviations = repair_call_result(
        result, index.tools["ListSales"], index, null_policy="drop"
    )

    assert repaired.structured_content is None
    assert len(repaired.content) == 2
    first, report = repaired.content
    assert isinstance(first, TextContent) and isinstance(report, TextContent)
    assert json.loads(first.text) == {"data": [{"id": "5"}]}
    assert json.loads(report.text) == {DEVIATIONS_KEY: deviations}


def test_repair_call_result_caps_deviations_and_counts_overflow() -> None:
    index = build_schema_index(_SPEC)
    result = _sales_result({"data": [{"tags": [None] * (MAX_DEVIATIONS + 10)}]})

    repaired, deviations = repair_call_result(
        result, index.tools["ListSales"], index, null_policy="drop"
    )

    assert len(deviations) == MAX_DEVIATIONS
    assert isinstance(repaired.structured_content, dict)
    assert repaired.structured_content[DEVIATIONS_TRUNCATED_KEY] == 10
    assert repaired.structured_content["data"] == [{"tags": []}]


# ---------------------------------------------------------------------------
# Spec loading and cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_schema_index_loads_once_per_source(tmp_path: Path) -> None:
    spec_file = _write_spec(tmp_path)
    loads: list[str] = []
    original = output_repair._load_spec_document

    def counting(source: str) -> dict[str, Any]:
        loads.append(source)
        return original(source)

    with patch.object(output_repair, "_load_spec_document", counting):
        first = await get_schema_index(str(spec_file))
        second = await get_schema_index(str(spec_file))

    assert first is not None and first is second
    assert "ListSales" in first.tools
    assert loads == [str(spec_file)]


@pytest.mark.asyncio
async def test_get_schema_index_reads_yaml_specs(tmp_path: Path) -> None:
    spec_file = _write_spec(tmp_path, "spec.yaml")

    index = await get_schema_index(str(spec_file))

    assert index is not None
    assert set(index.tools) >= {"ListSales", "RefResp"}


@pytest.mark.asyncio
async def test_get_schema_index_caches_load_failures(tmp_path: Path) -> None:
    missing = str(tmp_path / "missing.json")
    loads: list[str] = []
    original = output_repair._load_spec_document

    def counting(source: str) -> dict[str, Any]:
        loads.append(source)
        return original(source)

    with patch.object(output_repair, "_load_spec_document", counting):
        assert await get_schema_index(missing) is None
        assert await get_schema_index(missing) is None

    assert loads == [missing]


# ---------------------------------------------------------------------------
# Dispatch entry point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_repair_output_applies_only_in_repair_mode(tmp_path: Path) -> None:
    spec_file = _write_spec(tmp_path)
    payload = {"data": [{"id": 1}]}

    for mode in ("strict", "off"):
        service = _service_with(_openapi_config(spec_file, output_validation=mode))
        result = _sales_result(payload)
        untouched = await maybe_repair_output(
            service=service,
            server_name="lightspeed",
            tool_name="ListSales",
            result=result,
            project_id=_PROJECT_ID,
        )
        assert untouched is result

    service = _service_with(_openapi_config(spec_file, output_validation="repair"))
    repaired = await maybe_repair_output(
        service=service,
        server_name="lightspeed",
        tool_name="ListSales",
        result=_sales_result(payload),
        project_id=_PROJECT_ID,
    )

    assert isinstance(repaired, CallToolResult)
    assert repaired.structured_content == {
        "data": [{"id": "1"}],
        DEVIATIONS_KEY: [
            {"path": "$.data[0].id", "expected": "string", "actual": "integer", "action": "coerced"}
        ],
    }


@pytest.mark.asyncio
async def test_maybe_repair_output_honours_empty_null_policy(tmp_path: Path) -> None:
    spec_file = _write_spec(tmp_path)
    service = _service_with(
        _openapi_config(spec_file, output_validation="repair", repair_null_policy="empty")
    )

    repaired = await maybe_repair_output(
        service=service,
        server_name="lightspeed",
        tool_name="ListSales",
        result=_sales_result({"data": [{"id": None}]}),
        project_id=_PROJECT_ID,
    )

    assert isinstance(repaired, CallToolResult)
    assert repaired.structured_content is not None
    assert repaired.structured_content["data"] == [{"id": ""}]


@pytest.mark.asyncio
async def test_maybe_repair_output_skips_errors_dicts_and_unknown_tools(tmp_path: Path) -> None:
    spec_file = _write_spec(tmp_path)
    service = _service_with(_openapi_config(spec_file, output_validation="repair"))
    error_result = _sales_result({"data": [{"id": 1}]}, is_error=True)
    dict_result = {"success": False, "error": "boom"}
    unknown_tool_result = _sales_result({"data": [{"id": 1}]})

    assert (
        await maybe_repair_output(
            service=service,
            server_name="lightspeed",
            tool_name="ListSales",
            result=error_result,
            project_id=_PROJECT_ID,
        )
        is error_result
    )
    assert (
        await maybe_repair_output(
            service=service,
            server_name="lightspeed",
            tool_name="ListSales",
            result=dict_result,
            project_id=_PROJECT_ID,
        )
        is dict_result
    )
    assert (
        await maybe_repair_output(
            service=service,
            server_name="lightspeed",
            tool_name="NoSuchTool",
            result=unknown_tool_result,
            project_id=_PROJECT_ID,
        )
        is unknown_tool_result
    )


@pytest.mark.asyncio
async def test_maybe_repair_output_falls_open_on_internal_errors(tmp_path: Path) -> None:
    spec_file = _write_spec(tmp_path)
    service = _service_with(_openapi_config(spec_file, output_validation="repair"))
    result = _sales_result({"data": [{"id": 1}]})

    with patch.object(output_repair, "resolve_server", side_effect=RuntimeError("boom")):
        untouched = await maybe_repair_output(
            service=service,
            server_name="lightspeed",
            tool_name="ListSales",
            result=result,
            project_id=_PROJECT_ID,
        )

    assert untouched is result


@pytest.mark.asyncio
async def test_tool_proxy_call_tool_repairs_openapi_instance_results(tmp_path: Path) -> None:
    spec_file = _write_spec(tmp_path)
    manager = _manager_with(_openapi_config(spec_file, output_validation="repair"))
    manager.call_tool = AsyncMock(return_value=_sales_result({"data": [{"id": 1, "count": None}]}))
    internal = MagicMock()
    internal.is_internal.return_value = False
    internal.find_tool_server.return_value = None
    proxy = ToolProxyService(
        mcp_manager=manager, internal_manager=internal, validate_arguments=False
    )

    result = await proxy.call_tool("lightspeed", "ListSales", {}, project_id=_PROJECT_ID)

    assert isinstance(result, CallToolResult)
    assert result.structured_content == {
        "data": [{"id": "1"}],
        DEVIATIONS_KEY: [
            {
                "path": "$.data[0].id",
                "expected": "string",
                "actual": "integer",
                "action": "coerced",
            },
            {
                "path": "$.data[0].count",
                "expected": "integer",
                "actual": "null",
                "action": "dropped",
            },
        ],
    }
    manager.call_tool.assert_awaited_once()
