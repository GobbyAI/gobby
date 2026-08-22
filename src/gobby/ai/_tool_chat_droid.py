"""Droid stream-JSON-RPC adapter with an isolated shared-runtime MCP server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from gobby.agents.spawn_cache_policy import merge_spawn_path
from gobby.ai._text_generation_adapters import (
    _droid_isolated_env,
    _seed_droid_factory_state,
)
from gobby.ai._tool_chat_contracts import (
    LIMIT_STOP_REASONS,
    TIMEOUT_STOP_REASON,
    ToolChatRequest,
    ToolChatResult,
)
from gobby.ai._tool_chat_mcp_server import ToolLoopController, ToolRuntimeMCPServer
from gobby.ai._tool_chat_tools import ToolRuntime, validate_policy
from gobby.ai.registry import (
    AICapability,
    CapabilityBinding,
    CapabilityUnavailableError,
)

logger = logging.getLogger(__name__)

_FACTORY_API_VERSION = "1.0.0"
_FACTORY_PROTOCOL_VERSION = "1.131.0"
_REQUIRED_PROTOCOL = (1, 131, 0)
_SESSION_NOTIFICATION = "droid.session_notification"
_NATIVE_TOOL_METHODS = frozenset({"droid.request_permission", "droid.ask_user"})


class DroidProtocolError(RuntimeError):
    """Raised when Droid cannot satisfy the required JSON-RPC contract."""


class DroidRpcClientProtocol(Protocol):
    def set_notification_observer(
        self,
        observer: Callable[[dict[str, Any]], None],
    ) -> None: ...

    async def start(self) -> None: ...

    async def request(self, method: str, params: Mapping[str, object]) -> object: ...

    async def next_notification(self) -> dict[str, Any]: ...

    async def stop(self) -> None: ...


DroidClientFactory = Callable[..., DroidRpcClientProtocol]


def _version_tuple(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise DroidProtocolError("Droid response omitted factoryProtocolVersion")
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise DroidProtocolError(f"invalid Droid factoryProtocolVersion {value!r}")
    version = (int(parts[0]), int(parts[1]), int(parts[2]))
    if version[0] != _REQUIRED_PROTOCOL[0] or version < _REQUIRED_PROTOCOL:
        raise DroidProtocolError(
            f"Droid protocol {value} is unsupported; require {_FACTORY_PROTOCOL_VERSION} or newer"
        )
    return version


def _request_message(
    request_id: str,
    method: str,
    params: Mapping[str, object],
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "factoryApiVersion": _FACTORY_API_VERSION,
        "factoryProtocolVersion": _FACTORY_PROTOCOL_VERSION,
        "type": "request",
        "id": request_id,
        "method": method,
        "params": dict(params),
    }


class DroidRpcClient:
    """Minimal JSON-lines client for Droid's stream-JSON-RPC exec mode."""

    def __init__(
        self,
        *,
        command_path: str,
        cwd: Path,
        env: Mapping[str, str],
    ) -> None:
        self._command_path = command_path
        self._cwd = cwd
        self._env = dict(env)
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[object]] = {}
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._notification_observer: Callable[[dict[str, Any]], None] | None = None
        self._write_lock = asyncio.Lock()
        self._stderr: list[str] = []

    def set_notification_observer(
        self,
        observer: Callable[[dict[str, Any]], None],
    ) -> None:
        self._notification_observer = observer

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            self._command_path,
            "exec",
            "--input-format",
            "stream-jsonrpc",
            "--output-format",
            "stream-jsonrpc",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def request(self, method: str, params: Mapping[str, object]) -> object:
        process = self._require_process()
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[object] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._write(_request_message(request_id, method, params))
            return await future
        finally:
            self._pending.pop(request_id, None)
            if process.returncode is not None and not future.done():
                future.cancel()

    async def next_notification(self) -> dict[str, Any]:
        return await self._notifications.get()

    async def stop(self) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            try:
                await asyncio.wait_for(
                    self.request("droid.close_session", {"reason": "tool_chat complete"}),
                    timeout=1.0,
                )
            except (TimeoutError, RuntimeError, DroidProtocolError):
                pass
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        self._process = None
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task is not None),
            return_exceptions=True,
        )
        self._reader_task = None
        self._stderr_task = None
        self._fail_pending(DroidProtocolError("Droid JSON-RPC client stopped"))

    async def _read_stdout(self) -> None:
        process = self._require_process()
        stdout = process.stdout
        if stdout is None:
            raise DroidProtocolError("Droid stdout pipe is unavailable")
        try:
            while line := await stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DroidProtocolError("Droid emitted invalid stream-JSON-RPC") from exc
                if not isinstance(message, dict):
                    raise DroidProtocolError("Droid emitted a non-object JSON-RPC message")
                _version_tuple(message.get("factoryProtocolVersion"))
                await self._dispatch(message)
            stderr = "".join(self._stderr).strip()
            suffix = f": {stderr}" if stderr else ""
            raise DroidProtocolError(f"Droid JSON-RPC stream closed unexpectedly{suffix}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_pending(exc)
            await self._notifications.put({"_protocol_error": exc})

    async def _read_stderr(self) -> None:
        process = self._require_process()
        stderr = process.stderr
        if stderr is None:
            return
        while line := await stderr.readline():
            self._stderr.append(line.decode(errors="replace"))

    async def _dispatch(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        request_id = message.get("id")
        if message_type == "response" and isinstance(request_id, str):
            future = self._pending.get(request_id)
            if future is None or future.done():
                return
            error = message.get("error")
            if error is not None:
                future.set_exception(DroidProtocolError(f"Droid JSON-RPC error: {error}"))
            else:
                future.set_result(message.get("result"))
            return
        if message_type == "request" and isinstance(request_id, str):
            method = message.get("method")
            if method in _NATIVE_TOOL_METHODS:
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "factoryApiVersion": _FACTORY_API_VERSION,
                        "factoryProtocolVersion": _FACTORY_PROTOCOL_VERSION,
                        "type": "response",
                        "id": request_id,
                        "error": {
                            "code": -32000,
                            "message": "Native Droid interactions are disabled for tool_chat",
                        },
                    }
                )
                await self._notifications.put(
                    {
                        "_protocol_error": DroidProtocolError(
                            f"Droid attempted disabled native interaction {method}"
                        )
                    }
                )
                return
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "factoryApiVersion": _FACTORY_API_VERSION,
                    "factoryProtocolVersion": _FACTORY_PROTOCOL_VERSION,
                    "type": "response",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    },
                }
            )
            return
        if message.get("method") == _SESSION_NOTIFICATION:
            if self._notification_observer is not None:
                self._notification_observer(message)
            await self._notifications.put(message)

    async def _write(self, payload: Mapping[str, object]) -> None:
        process = self._require_process()
        stdin = process.stdin
        if stdin is None:
            raise DroidProtocolError("Droid stdin pipe is unavailable")
        encoded = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        async with self._write_lock:
            stdin.write(encoded)
            await stdin.drain()

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise DroidProtocolError("Droid JSON-RPC process is not running")
        return self._process

    def _fail_pending(self, error: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)


def _capability_error(
    binding: CapabilityBinding,
    model: str | None,
    reason: str,
) -> CapabilityUnavailableError:
    return CapabilityUnavailableError(
        AICapability.TOOL_CHAT,
        provider=binding.provider,
        model=model,
        reason=f"Droid stream-JSON-RPC contract unavailable: {reason}",
    )


def _assistant_text(notification: Mapping[str, object]) -> tuple[bool, str]:
    message = notification.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return False, ""
    content = message.get("content")
    if not isinstance(content, list):
        return True, ""
    chunks = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return True, "".join(chunks)


def _native_tool_ids(catalog: object) -> list[str]:
    if not isinstance(catalog, dict) or not isinstance(catalog.get("tools"), list):
        raise DroidProtocolError("droid.list_tools returned an invalid catalog")
    tool_ids: list[str] = []
    for tool in catalog["tools"]:
        if not isinstance(tool, dict) or not isinstance(tool.get("id"), str):
            raise DroidProtocolError("droid.list_tools returned an invalid tool entry")
        tool_ids.append(tool["id"])
    return tool_ids


def _assert_native_tools_disabled(catalog: object) -> None:
    if not isinstance(catalog, dict) or not isinstance(catalog.get("tools"), list):
        raise DroidProtocolError("droid.list_tools returned an invalid catalog")
    allowed = [
        tool.get("id")
        for tool in catalog["tools"]
        if isinstance(tool, dict) and tool.get("currentlyAllowed") is True
    ]
    if allowed:
        raise DroidProtocolError(f"native Droid tools remain enabled: {allowed}")


class DroidSpawnToolChatAdapter:
    """Run Droid through its enforceable JSON-RPC and MCP contracts."""

    def __init__(
        self,
        *,
        command_path: str | None = None,
        client_factory: DroidClientFactory = DroidRpcClient,
    ) -> None:
        self._command_path = command_path
        self._client_factory = client_factory

    def _resolve_command_path(self) -> str:
        import shutil

        path = self._command_path or shutil.which("droid")
        if not path:
            raise FileNotFoundError("Droid CLI not found in PATH")
        return path

    async def chat(
        self,
        request: ToolChatRequest,
        binding: CapabilityBinding,
    ) -> ToolChatResult:
        validate_policy(request.tool_policy)
        limits = request.effective_limits
        runtime = ToolRuntime(
            request.tool_policy,
            project_path=request.project_path,
            limits=limits,
            builtins=request.builtins,
            subprocess_env=request.managed_subprocess_env,
            managed_execution_id=request.managed_execution_id,
        )
        controller = ToolLoopController(limits)
        model = request.model or next(iter(binding.models), None)
        text = ""
        saw_work = False

        with tempfile.TemporaryDirectory(prefix="tool-chat-droid-") as work_str:
            work = Path(work_str)
            temp_home = work / "home"
            temp_home.mkdir(parents=True, exist_ok=True)
            base_env = os.environ.copy()
            _seed_droid_factory_state(base_env, temp_home)
            isolated_env = _droid_isolated_env(base_env, temp_home)
            isolated_env.update(request.managed_subprocess_env)
            isolated_env["PATH"] = merge_spawn_path(isolated_env.get("PATH"))
            client = self._client_factory(
                command_path=self._resolve_command_path(),
                cwd=work,
                env=isolated_env,
            )

            def observe_turn(event: dict[str, Any]) -> None:
                params = event.get("params")
                notification = params.get("notification") if isinstance(params, dict) else None
                if isinstance(notification, dict):
                    is_assistant, _ = _assistant_text(notification)
                    if notification.get("type") == "create_message" and is_assistant:
                        controller.record_turn()

            client.set_notification_observer(observe_turn)
            server = ToolRuntimeMCPServer(runtime, controller)
            deadline = asyncio.get_running_loop().time() + limits.loop_timeout_seconds

            async def within_deadline[T](operation: Awaitable[T]) -> T:
                async with asyncio.timeout_at(deadline):
                    return await operation

            try:
                await within_deadline(server.start())
                if server.url is None:
                    raise RuntimeError("Droid tool runtime MCP server did not start")
                await within_deadline(client.start())

                async def interrupt() -> None:
                    await within_deadline(client.request("droid.interrupt_session", {}))

                controller.set_interrupt(interrupt)
                init_params: dict[str, object] = {
                    "machineId": platform.node() or "gobby-tool-chat",
                    "cwd": request.project_path,
                    "mcpServers": [
                        {
                            "type": "http",
                            "name": "gobby-tool-loop",
                            "url": server.url,
                            "headers": [
                                {
                                    "name": "Authorization",
                                    "value": server.authorization_header,
                                }
                            ],
                        }
                    ],
                    "skipPermissionsUnsafe": True,
                }
                if model is not None:
                    init_params["modelId"] = model
                if request.reasoning_effort not in {None, "auto"}:
                    init_params["reasoningEffort"] = request.reasoning_effort
                await within_deadline(client.request("droid.initialize_session", init_params))

                catalog = await within_deadline(client.request("droid.list_tools", {}))
                native_ids = _native_tool_ids(catalog)
                await within_deadline(
                    client.request(
                        "droid.update_session_settings",
                        {"enabledToolIds": [], "disabledToolIds": native_ids},
                    )
                )
                _assert_native_tools_disabled(
                    await within_deadline(client.request("droid.list_tools", {}))
                )

                prompt = request.prompt
                if request.system_prompt:
                    prompt = f"{request.system_prompt}\n\n{request.prompt}"
                await within_deadline(client.request("droid.add_user_message", {"text": prompt}))
                while True:
                    event = await within_deadline(client.next_notification())
                    protocol_error = event.get("_protocol_error")
                    if isinstance(protocol_error, Exception):
                        raise protocol_error
                    params = event.get("params")
                    if not isinstance(params, dict):
                        continue
                    notification = params.get("notification")
                    if not isinstance(notification, dict):
                        continue
                    event_type = notification.get("type")
                    if event_type == "create_message":
                        is_assistant, candidate = _assistant_text(notification)
                        if is_assistant:
                            if candidate:
                                text = candidate
                    elif event_type == "droid_working_state_changed":
                        state = notification.get("newState")
                        if state == "idle" and saw_work:
                            break
                        if state != "idle":
                            saw_work = True
                    elif event_type == "error":
                        raise RuntimeError(f"Droid tool_chat error: {notification.get('message')}")
            except TimeoutError:
                controller.stop_reason = TIMEOUT_STOP_REASON
            except DroidProtocolError as exc:
                raise _capability_error(binding, model, str(exc)) from exc
            finally:
                try:
                    await client.stop()
                finally:
                    await server.stop()

        stop_reason = controller.stop_reason or "completed"
        if stop_reason == "completed" and not text:
            raise RuntimeError(
                "Droid tool_chat produced no final message "
                f"(model={model}, tool_use_count={runtime.calls_used})"
            )
        return ToolChatResult(
            text=text if stop_reason == "completed" else "",
            provider=binding.provider,
            model=model,
            tool_use_count=runtime.calls_used,
            turns=controller.turns,
            tools={
                name: sum(1 for item in runtime.invocation_log if item.get("tool_name") == name)
                for name in runtime.tool_names()
                if any(item.get("tool_name") == name for item in runtime.invocation_log)
            },
            applied_reasoning_effort=(
                request.reasoning_effort if request.reasoning_effort != "auto" else None
            ),
            stop_reason=stop_reason,
            trace=tuple(runtime.invocation_log),
            calls_used=runtime.calls_used,
            budget_exhausted=stop_reason in LIMIT_STOP_REASONS,
            trace_available=True,
        )
