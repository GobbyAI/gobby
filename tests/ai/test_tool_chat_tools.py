"""Unit tests for the tool_chat repo-investigation tool registry."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gobby.ai import BuiltinExecutionContext, BuiltinToolResult, BuiltinToolSpec
from gobby.ai import _tool_chat_tools as tools
from gobby.ai._tool_chat_contracts import ToolLoopLimits, ToolPolicy
from gobby.ai._tool_chat_tools import (
    ToolPolicyError,
    ToolRuntime,
    run_argv,
    tool_name_for,
    validate_policy,
)

pytestmark = pytest.mark.unit


def _readonly_gcode_policy() -> ToolPolicy:
    return ToolPolicy(cli="gcode", tools=("search", "outline", "symbol"))


def test_validate_policy_accepts_readonly_gcode() -> None:
    policy = _readonly_gcode_policy()
    validate_policy(policy)
    assert policy.tools == ("search", "outline", "symbol")


def test_validate_policy_rejects_unknown_cli() -> None:
    with pytest.raises(ToolPolicyError):
        validate_policy(ToolPolicy(cli="rm", tools=("search",)))


def test_validate_policy_rejects_empty_tools() -> None:
    with pytest.raises(ToolPolicyError):
        validate_policy(ToolPolicy(cli="gcode", tools=()))


def test_runtime_allows_builtin_only_policy() -> None:
    async def handler(
        arguments: dict[str, object], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        del arguments, context
        return BuiltinToolResult(payload={"ok": True})

    runtime = ToolRuntime(
        ToolPolicy(cli="gcode", tools=()),
        project_path="/repo",
        builtins=(
            BuiltinToolSpec(
                name="evidence",
                description="Read validation evidence.",
                input_schema={"type": "object"},
                handler=handler,
            ),
        ),
    )

    assert [schema["function"]["name"] for schema in runtime.openai_schemas()] == ["evidence"]


def test_validate_policy_rejects_mutator_without_allow_mutation() -> None:
    with pytest.raises(ToolPolicyError):
        validate_policy(ToolPolicy(cli="gcode", tools=("search", "index")))


@pytest.mark.asyncio
async def test_graph_view_is_readonly_without_graph_mutators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str]] = []

    async def fake_run_argv(
        argv: list[str],
        *,
        cwd: str,
        timeout: float,
        byte_cap: int,
        env: dict[str, str] | None = None,
    ) -> str:
        del cwd, timeout, byte_cap, env
        captured.append(argv)
        return "ok"

    monkeypatch.setattr(tools, "run_argv", fake_run_argv)
    policy = ToolPolicy(cli="gcode", tools=("callees", "graph"))
    validate_policy(policy)
    runtime = ToolRuntime(policy, project_path="/tmp")

    assert await runtime.execute("gcode_callees", {"args": ["Symbol"]}) == "ok"
    assert (
        await runtime.execute(
            "gcode_graph",
            {"args": ["view", "--view", "fcg", "Derived"]},
        )
        == "ok"
    )
    assert captured == [
        ["gcode", "callees", "Symbol"],
        ["gcode", "graph", "view", "--view", "fcg", "Derived"],
    ]

    for args in (
        ["clear"],
        ["rebuild"],
        ["sync-file", "--file", "src/app.py"],
        ["cleanup-orphans"],
    ):
        with pytest.raises(ToolPolicyError):
            await runtime.execute("gcode_graph", {"args": args})
    assert len(captured) == 2


def test_validate_policy_allows_mutator_when_opted_in() -> None:
    policy = ToolPolicy(cli="gwiki", tools=("search", "compile"), allow_mutation=True)
    validate_policy(policy)
    assert policy.allow_mutation is True


def test_validate_policy_rejects_codewiki_when_mutation_enabled() -> None:
    with pytest.raises(
        ToolPolicyError,
        match="Subcommand gcode 'codewiki' is not allowed by policy",
    ):
        validate_policy(ToolPolicy(cli="gcode", tools=("codewiki",), allow_mutation=True))


def test_validate_policy_rejects_unlisted_tool_even_when_opted_in() -> None:
    with pytest.raises(ToolPolicyError):
        validate_policy(ToolPolicy(cli="gwiki", tools=("destroy",), allow_mutation=True))


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


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("[error: policy denied]", True),
        ('{"success": false, "error": "failed"}', True),
        ('{"ok": false}', True),
        ('{"error_code": "tool_failed"}', True),
        ('{"success": true}', False),
        ("plain output", False),
    ],
)
def test_tool_result_error_classification_is_shared_and_typed(
    result: str,
    expected: bool,
) -> None:
    assert tools.tool_result_is_error(result) is expected


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

    async def fake_run_argv(
        argv: list[str],
        *,
        cwd: str,
        timeout: float,
        byte_cap: int,
        env: dict[str, str] | None = None,
    ) -> str:
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["timeout"] = timeout
        captured["byte_cap"] = byte_cap
        return "RESULT"

    monkeypatch.setattr(tools, "run_argv", fake_run_argv)
    runtime = ToolRuntime(
        _readonly_gcode_policy(),
        project_path="/repo",
        limits=ToolLoopLimits(tool_timeout_seconds=12, max_bytes_per_tool_result=99),
    )
    result = await runtime.execute("gcode_search", {"args": ["auth handler", "--limit", "5"]})
    assert result == "RESULT"
    assert captured["argv"] == ["gcode", "search", "auth handler", "--limit", "5"]
    assert captured["cwd"] == "/repo"
    assert captured["timeout"] == 12.0
    assert captured["byte_cap"] == 99


@pytest.mark.asyncio
async def test_execute_passes_managed_bootstrap_to_gcode_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_env: dict[str, str] | None = None

    async def fake_run_argv(
        argv: list[str],
        *,
        cwd: str,
        timeout: float,
        byte_cap: int,
        env: dict[str, str] | None,
    ) -> str:
        nonlocal captured_env
        captured_env = env
        return "ok"

    monkeypatch.setattr(tools, "run_argv", fake_run_argv)
    runtime = ToolRuntime(
        _readonly_gcode_policy(),
        project_path="/repo",
        subprocess_env={"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/managed/bootstrap.json"},
    )

    result = await runtime.execute("gcode_search", {"args": ["query"]})

    assert result == "ok"
    assert captured_env == {"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/managed/bootstrap.json"}


@pytest.mark.asyncio
async def test_run_argv_managed_context_strips_operator_database_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_env: dict[str, str] | None = None

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"ok", b""

    async def create_process(*_argv: str, **kwargs: object) -> Process:
        nonlocal captured_env
        env = kwargs.get("env")
        assert isinstance(env, dict)
        captured_env = env
        return Process()

    monkeypatch.setenv("DATABASE_URL", "postgresql://operator/database-url")
    monkeypatch.setattr(
        "gobby.ai._tool_chat_tools.asyncio.create_subprocess_exec",
        create_process,
    )

    result = await run_argv(
        ["gcode", "status"],
        cwd=str(tmp_path),
        timeout=1,
        byte_cap=1024,
        env={"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/managed/bootstrap.json"},
    )

    assert result == "ok"
    assert captured_env is not None
    assert captured_env["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"] == "/managed/bootstrap.json"
    assert "DATABASE_URL" not in captured_env


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
    assert out == "x" * 10
    assert len(out.encode("utf-8")) == 10


@pytest.mark.asyncio
async def test_run_argv_stdout_marker_reports_utf8_trimmed_bytes(tmp_path: object) -> None:
    out = await run_argv(
        [
            sys.executable,
            "-c",
            'import sys; sys.stdout.buffer.write("é".encode() * 6)',
        ],
        cwd=str(tmp_path),
        timeout=5.0,
        byte_cap=11,
    )

    assert out.startswith("é" * 5)
    assert len(out.encode("utf-8")) <= 11


@pytest.mark.asyncio
async def test_run_argv_stderr_marker_reports_utf8_trimmed_tail_bytes(tmp_path: object) -> None:
    out = await run_argv(
        [
            sys.executable,
            "-c",
            'import sys; sys.stderr.buffer.write("é".encode() * 1024 + b"a"); sys.exit(2)',
        ],
        cwd=str(tmp_path),
        timeout=5.0,
        byte_cap=4096,
    )

    assert "[stderr: last 2047 of 2049 bytes (cap 2048)]" in out


@pytest.mark.asyncio
async def test_run_argv_preserves_under_cap_stderr_bytes(tmp_path: object) -> None:
    out = await run_argv(
        [
            sys.executable,
            "-c",
            'import sys; sys.stderr.buffer.write(b"alpha\\nbeta"); sys.exit(2)',
        ],
        cwd=str(tmp_path),
        timeout=5.0,
        byte_cap=4096,
    )

    assert out == "[exit 2: alpha\nbeta]"


@pytest.mark.asyncio
async def test_run_argv_missing_executable(tmp_path: object) -> None:
    out = await run_argv(
        ["gobby-no-such-binary-xyz"], cwd=str(tmp_path), timeout=5.0, byte_cap=4096
    )
    assert "not found" in out


@pytest.mark.asyncio
async def test_run_argv_reports_invalid_cwd_before_spawn(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    out = await run_argv(["/bin/echo", "hello"], cwd=str(missing), timeout=5.0, byte_cap=4096)

    assert "working directory not found" in out
    assert str(missing) in out


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
