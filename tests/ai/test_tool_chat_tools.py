"""Unit tests for the tool_chat repo-investigation tool registry."""

from __future__ import annotations

import pytest

from gobby.ai import _tool_chat_tools as tools
from gobby.ai._tool_chat_contracts import ToolLoopLimits, ToolPolicy
from gobby.ai._tool_chat_tools import (
    ToolPolicyError,
    ToolRuntime,
    run_argv,
    tool_name_for,
    validate_policy,
)


def _readonly_gcode_policy() -> ToolPolicy:
    return ToolPolicy(cli="gcode", tools=("search", "outline", "symbol"))


def test_validate_policy_accepts_readonly_gcode() -> None:
    validate_policy(_readonly_gcode_policy())  # does not raise


def test_validate_policy_rejects_unknown_cli() -> None:
    with pytest.raises(ToolPolicyError):
        validate_policy(ToolPolicy(cli="rm", tools=("search",)))


def test_validate_policy_rejects_empty_tools() -> None:
    with pytest.raises(ToolPolicyError):
        validate_policy(ToolPolicy(cli="gcode", tools=()))


def test_validate_policy_rejects_mutator_without_allow_mutation() -> None:
    with pytest.raises(ToolPolicyError):
        validate_policy(ToolPolicy(cli="gcode", tools=("search", "index")))


def test_validate_policy_allows_mutator_when_opted_in() -> None:
    validate_policy(ToolPolicy(cli="gwiki", tools=("search", "compile"), allow_mutation=True))


def test_validate_policy_rejects_metacharacter_subcommand() -> None:
    with pytest.raises(ToolPolicyError):
        validate_policy(ToolPolicy(cli="gcode", tools=("search; rm -rf /",)))


def test_openai_schemas_expose_policy_tools() -> None:
    runtime = ToolRuntime(_readonly_gcode_policy(), project_path="/tmp")
    schemas = runtime.openai_schemas()
    names = {schema["function"]["name"] for schema in schemas}
    assert names == {"gcode_search", "gcode_outline", "gcode_symbol"}
    for schema in schemas:
        assert schema["type"] == "function"
        params = schema["function"]["parameters"]
        assert params["additionalProperties"] is False
        assert params["properties"]["args"]["type"] == "array"


def test_tool_name_for_normalizes_hyphens() -> None:
    assert tool_name_for("gcode", "search-symbol") == "gcode_search_symbol"


def test_resolve_maps_tool_name_to_subcommand() -> None:
    runtime = ToolRuntime(ToolPolicy(cli="gcode", tools=("search-symbol",)), project_path="/tmp")
    assert runtime.resolve("gcode_search_symbol") == "search-symbol"


def test_resolve_rejects_tool_outside_policy() -> None:
    runtime = ToolRuntime(_readonly_gcode_policy(), project_path="/tmp")
    with pytest.raises(ToolPolicyError):
        runtime.resolve("gcode_index")


@pytest.mark.asyncio
async def test_execute_rejects_metacharacter_args() -> None:
    runtime = ToolRuntime(_readonly_gcode_policy(), project_path="/tmp")
    with pytest.raises(ToolPolicyError):
        await runtime.execute("gcode_search", {"args": ["foo; rm -rf /"]})


@pytest.mark.asyncio
async def test_execute_builds_argv_and_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_argv(argv: list[str], *, cwd: str, timeout: float, byte_cap: int) -> str:
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["timeout"] = timeout
        captured["byte_cap"] = byte_cap
        return "RESULT"

    monkeypatch.setattr(tools, "run_argv", fake_run_argv)
    runtime = ToolRuntime(
        _readonly_gcode_policy(),
        project_path="/repo",
        limits=ToolLoopLimits(tool_timeout_seconds=12.0, per_tool_result_byte_cap=99),
    )
    result = await runtime.execute("gcode_search", {"args": ["auth handler", "--limit", "5"]})
    assert result == "RESULT"
    assert captured["argv"] == ["gcode", "search", "auth handler", "--limit", "5"]
    assert captured["cwd"] == "/repo"
    assert captured["timeout"] == 12.0
    assert captured["byte_cap"] == 99


@pytest.mark.asyncio
async def test_execute_denied_call_runs_no_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def fake_run_argv(*args: object, **kwargs: object) -> str:
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(tools, "run_argv", fake_run_argv)
    runtime = ToolRuntime(_readonly_gcode_policy(), project_path="/repo")
    with pytest.raises(ToolPolicyError):
        await runtime.execute("gcode_index", {"args": []})
    assert called is False


@pytest.mark.asyncio
async def test_run_argv_captures_output(tmp_path: object) -> None:
    out = await run_argv(
        ["/bin/echo", "hello world"], cwd=str(tmp_path), timeout=5.0, byte_cap=4096
    )
    assert out.strip() == "hello world"


@pytest.mark.asyncio
async def test_run_argv_caps_bytes(tmp_path: object) -> None:
    out = await run_argv(["/bin/echo", "x" * 100], cwd=str(tmp_path), timeout=5.0, byte_cap=10)
    assert "[output truncated]" in out
    # 10 bytes of payload kept before the truncation marker.
    assert out.split("\n")[0] == "x" * 10


@pytest.mark.asyncio
async def test_run_argv_missing_executable(tmp_path: object) -> None:
    out = await run_argv(
        ["gobby-no-such-binary-xyz"], cwd=str(tmp_path), timeout=5.0, byte_cap=4096
    )
    assert "not found" in out


@pytest.mark.asyncio
async def test_run_argv_nonzero_exit_surfaces_stderr(tmp_path: object) -> None:
    out = await run_argv(
        ["/bin/sh", "-c", "echo oops 1>&2; exit 3"],
        cwd=str(tmp_path),
        timeout=5.0,
        byte_cap=4096,
    )
    assert "exit 3" in out
    assert "oops" in out
